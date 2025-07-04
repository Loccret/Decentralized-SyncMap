# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/04_CudaSyncMap.ipynb.

# %% auto 0
__all__ = ['VariableTrackerV2', 'NodeSyncMap', 'data_preprocessing']

# %% ../nbs/04_CudaSyncMap.ipynb 2
import copy
import math
import pprint
import re
import sys
import time
import warnings
from collections import deque

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import seaborn as sns
import torch
from fastcore.utils import *
from line_profiler import LineProfiler
from scipy.spatial import distance
from scipy.stats import entropy, wasserstein_distance
from sklearn.cluster import DBSCAN
from sklearn.manifold import TSNE
from sklearn.metrics import normalized_mutual_info_score, pairwise_distances
from tqdm import tqdm
import optuna
from optuna.samplers import TPESampler
# 如果没有 pip install -e . 下面一行就不会成功
from .processor import (GraphProcessor, Readout,
                                     WorkingMemProcessor)
from .utility import (OverlapChunkTest1, compute_combi_dist, compute_nmi, 
                                   convert_rgb_list_to_hex, create_trace_plot,
                                   labels2colors, reduce_dimension_with_tsne,
                                   to_categorical)

os.environ['LINE_PROFILE'] = '1'
np.set_printoptions(suppress=True)  # disable scientific notation

# %% ../nbs/04_CudaSyncMap.ipynb 7
class VariableTrackerV2:
    def __init__(self, max_length, vars, device='cuda'):
        self.K = max_length  # Number of steps to remember
        self.N = vars        # Number of variables
        # Initialize buffer of shape (N, K, N) to store relationships
        self.buffer = torch.zeros((self.N, self.K, self.N), dtype=torch.bool, device=device)

    def write(self, x):
        N, K = self.N, self.K
        x_indices = x.nonzero(as_tuple=False).view(-1)
        # Shift the buffer for all variables along the K dimension
        self.buffer[x_indices, 1:] = self.buffer[x_indices, :-1].clone()
        # self.buffer[:, 0, :] = torch.zeros((N, N), dtype=torch.bool, device='cuda')

        
        # # Create a copy of x and exclude self-relationships
        # x_expanded = x.unsqueeze(0).expand(N, N)
        x_expanded = x.unsqueeze(0).repeat(N, 1)
        # x_expanded.fill_diagonal_(False)
        # # Update buffer for variables where x[i] is True
        x_mask = x.view(N, 1)
        # self.buffer[:, 0, :] = torch.where(x_mask, x_expanded, self.buffer[:, 0, :])
        
        self.buffer[x_indices, 0, :] = (x_mask * x_expanded)[x_indices]

    def read(self, y):
        # N, K = self.N, self.K
        # Identify indices where y is True
        y_indices = y.nonzero(as_tuple=False).view(-1)
        # Retrieve stored relationships for these variables, excluding the newest record
        buffers = self.buffer[y_indices] #[:, 1:, :]  # Shape: (len(y_indices), K-1, N)
        # Flatten the buffers along the K dimension
        # buffers = buffers.view(-1, N)
        return buffers


# %% ../nbs/04_CudaSyncMap.ipynb 9
class NodeSyncMap(object):
    def __init__(self, input_size, dimensions, adaptation_rate, 
                plus_factor = 1, minus_factor = 0.1, attract_range = 0.0001, 
                repel_range = 1,plus_exp_factor = 0.2, minus_exp_factor = 1,
                history_repel_factor = 1000, history_repel_multiplier = 1, max_track_length = 2,
                max_past_activate = 0.001, use_tqdm = True, normalization = False, 
                std_factor = 2, fix_seed = True, device = 'cpu', 
                ):
        if device == 'cuda':
            device = 'cuda:0'
        self.device = torch.device(device)
        if fix_seed:
            np.random.seed(42)
        self.syncmap= torch.tensor(
            np.random.rand(input_size,dimensions).astype(np.float32),
            dtype=torch.float32, device=self.device)


        self.organized= False
        self.input_size= input_size
        self.dimensions= dimensions
        self.adaptation_rate= adaptation_rate
        self.use_tqdm = use_tqdm

        self.plus_factor = plus_factor
        self.minus_factor = minus_factor

        self.attract_range =  attract_range
        self.repel_range = repel_range
        
        self.plus_exp_factor = plus_exp_factor
        self.minus_exp_factor = minus_exp_factor

        self.history_repel_factor = history_repel_factor
        self.history_repel_multiplier = history_repel_multiplier
        # 0.5
        # self.trajectory_attract = 10
        
        self.history_repel = torch.zeros((input_size, input_size), device=device)
        # self.variable_tracker = VariableTracker(vars = input_size, max_length = max_track_length, device=device)

        self.variable_tracker = VariableTrackerV2(vars = input_size, max_length = max_track_length, device=device)
        self.max_past_activate = max_past_activate
        self.normalization = normalization
        self.std_factor = std_factor


        # 
        self.lengths = max_track_length
        self.first_ele = torch.tensor([1], device=self.device, dtype=torch.float32)
        self.fit_log = []
        # self.clear = torch.cuda.empty_cache if device == 'cuda' else list


    def calculate_pairwise_distances(self, arr):
        """
        Calculate the pairwise distances between samples in a NumPy array.

        Parameters:
        arr (np.ndarray): A 2D NumPy array of shape (N, d) where N is the number of samples
                            and d is the dimension of each sample.

        Returns:
        np.ndarray: A 2D NumPy array of shape (N, N) where each element (i, j) represents
                    the distance between sample i and sample j.
        """

        # Calculate the pairwise distances
        diff = arr.unsqueeze(0) - arr.unsqueeze(1)
        squared_diff = diff ** 2
        squared_dist = torch.sum(squared_diff, dim=-1)
        distances = torch.sqrt(squared_dist)
        return distances

    @property  # 在class内部引用这个函数贼慢
    def log(self):
        # return torch.tensor(self.fit_log, dtype=torch.float32)
        return torch.stack(self.fit_log)


    def maybe_tqdm(self, iterable, total=None, use_tqdm=True):
        if use_tqdm:
            return tqdm(iterable, total=total)
        return iterable


    def fit(self, input_sequence):
        self.fit_log = torch.zeros((10+len(input_sequence) // 10, self.input_size, self.dimensions), device=self.device)


        # for idx, state,in self.maybe_tqdm(
        #     enumerate(input_sequence), total=len(input_sequence), use_tqdm=self.use_tqdm):

        #     vplus, vminus = self.get_postive_and_negative_state(state)
            
        #     new_snycmap = self.one_step_organize(vplus, vminus, idx)

        #     if new_snycmap is not None:
        #         self.syncmap = new_snycmap

        with torch.no_grad():
            for idx, (vplus, vminus) in self.maybe_tqdm(
                enumerate(zip(input_sequence, input_sequence.logical_not())), 
                total=len(input_sequence), use_tqdm=self.use_tqdm):
                # vplus, vminus = self.get_postive_and_negative_state(state)
                
                new_snycmap = self.one_step_organize(vplus, vminus, idx)
                # check whether the allocated memory greater than 22GB
                if self.device != torch.device('cpu'):
                    free, total = torch.cuda.mem_get_info(torch.device(self.device))
                    mem_used_MB = (total - free) / 1024 ** 2
                    if mem_used_MB > 23000:
                        torch.cuda.empty_cache()
                if new_snycmap is not None:
                    self.syncmap = new_snycmap


    def get_center(self, arr, syncmap_arr):
        '''
        calculate the mass center of the input arrays.
        If the activated or deactivated state just one sample, return  None
        args:
            arr(d): np.array, the element is boolean representing the activated and deactivated variables
        '''
        return (arr.float() @ syncmap_arr) / arr.sum()  # (D,) @ (D, d) -> (d,)


    def get_postive_and_negative_state(self, state):
        '''
            adentify the state whether is activated and deactivated
        args:
            state(d): np.ndarray, the one step input sequence, d is the number of feature of input
        return:
            plus, minus(D): np.ndarray, th elements are boolean representing the activated and deactivated state
        '''
        plus= state > 0.1
        minus = ~ plus
        return plus, minus


    def exp_update(self, dist, affective_range):
        return torch.exp(- dist / affective_range)

    def repel_constant_update(self, plus_mask, minus_mask):
        # 排斥重来没有连接过的
        '''
            return: (N, N, D)
        '''
        self.history_repel += minus_mask
        self.history_repel[plus_mask.bool()] = 0
        self.history_repel = torch.minimum(self.history_repel, self.history_repel_factor * torch.ones_like(self.history_repel).to(self.device))
        return self.history_repel.unsqueeze(2) / self.history_repel_factor
    
        # self.history_repel = np.minimum(self.history_repel, 1000)
        # return self.history_repel[:,:,np.newaxis]/1000
        
    def compute_update(self, syncmap_previous, vplus, vminus):
        # last_update_plus = None
        all2all_distance = self.calculate_pairwise_distances(syncmap_previous)  # (N, N)
        all_coordinate_diff = syncmap_previous.unsqueeze(0) - syncmap_previous.unsqueeze(1)  # 这个方向代表了排斥 (N, N, D)
        
        # with torch.no_grad():
        all_coordinate_diff_sin_cos = torch.nan_to_num(all_coordinate_diff / all2all_distance.unsqueeze(2), nan=0)
        # ** 2是为了引入距离关系， ** 1仅仅是方向向量 （N, N, D）
        # 在sum之前乘以系数可以为每个variable的排斥力加权； 

        # distance weighted plus update
        plus_mask  = vplus.unsqueeze(1).float() @ vplus.unsqueeze(0).float()  # 防止正向对负向产生影响
        # self.variable_tracker.write(vplus)
        # vplus_np = vplus.cpu().numpy()
        self.variable_tracker.write(vplus)
        # plus_idx = torch.nonzero(vplus.float(), as_tuple=False).squeeze()
        plus_idx = vplus.nonzero(as_tuple=False).view(-1)


        # parallelize the above for loop
        # history_vplus_stack = torch.tensor(
        #     np.array(self.variable_tracker.read(plus_idx)), dtype=torch.float32, device=self.device)  # (len(plus_idx), T, N)
        history_vplus_stack = self.variable_tracker.read(plus_idx)  # (len(plus_idx), T, N)
        history_plus_mask = history_vplus_stack.unsqueeze(3).float() @ history_vplus_stack.unsqueeze(2).float()  # (B, T, N, N)
        # lengths = history_vplus_stack.size(1)
        weights = torch.linspace(
            self.max_past_activate, 0, self.lengths, 
            device=self.device, dtype=torch.float32)[:-1]  # (T,) +2: 排除尾部
        weights = torch.cat([self.first_ele, weights])
        weights = weights.view(1, -1, 1, 1)  # (1, T, 1, 1) for broadcasting
        history_plus_weight = history_plus_mask * weights  # (B, T, N, N)

        total_plus_mask = history_plus_mask.sum(dim=(0, 1)).bool()  # (N, N)
        total_plus_weight = history_plus_weight.sum(dim=(0, 1))  # (N, N)


        # # total_plus_mask   += plus_mask # current plus
        # total_plus_mask = torch.logical_or(total_plus_mask, plus_mask)
        # total_plus_weight += plus_mask.float()  # current plus

        update_plus = -all_coordinate_diff_sin_cos * total_plus_weight.unsqueeze(2) * (
            1 + self.plus_exp_factor * self.exp_update(all2all_distance.unsqueeze(2), self.attract_range)
        )
        update_plus[~total_plus_mask] = 0
        update_plus = update_plus.sum(dim=0)

        # distance weighted minus update
        minus_mask = vminus.unsqueeze(1).float() @ vminus.unsqueeze(0).float()
        # update_minus = all_coordinate_diff_sin_cos * (
        #     self.minus_exp_factor * self.exp_update(all2all_distance.unsqueeze(2), self.repel_range) + self.repel_constant_update(plus_mask, minus_mask)
        # )
        # ########### 2024-08-09 ##############
        update_minus = all_coordinate_diff_sin_cos * (
            self.minus_exp_factor * self.exp_update(all2all_distance.unsqueeze(2), self.repel_range))
        # ########### 2024-08-09 ##############
        update_minus[~minus_mask.bool()] = 0  # ~minus_mask: 所有含有正向的variables update全部置零
        # ########### 2024-08-09 ############################
        update_minus += all_coordinate_diff_sin_cos * self.repel_constant_update(plus_mask, minus_mask) * self.history_repel_multiplier
        # ########### 2024-08-09 ############################
        update_minus = update_minus.sum(dim=0)
        return self.plus_factor * update_plus, self.minus_factor * update_minus


    def one_step_organize(self, vplus, vminus, current_state_idx):
        syncmap_previous = self.syncmap
        # UPDATE BY USING ALL-TO-ALL
        update_plus, update_minus = self.compute_update(syncmap_previous, vplus, vminus)
        update_value = update_plus + update_minus  # 确实是加号
        new_syncmap = self.syncmap.clone()
        new_syncmap += self.adaptation_rate * update_value
        if self.normalization:
            new_syncmap = (new_syncmap - new_syncmap.mean()) / (self.std_factor * new_syncmap.std(correction = 0) + 1e-10) 
        # new_syncmap = self.max_move_distance_test(syncmap_previous, new_syncmap, vplus, vminus, center_plus, center_minus, dist_plus, dist_minus)

        # new_syncmap = new_syncmap / np.abs(new_syncmap).max()

        if current_state_idx % 10 == 0:

            # self.fit_log.append(new_syncmap)
            # self.fit_log[current_state_idx // 10] = new_syncmap  # later

            current_state_idx_per_10 = current_state_idx // 10

            index = current_state_idx_per_10 * torch.ones(
                (self.input_size, self.dimensions), dtype=torch.long, device=new_syncmap.device)
            
            self.fit_log.scatter_(0, index.unsqueeze(0).expand(current_state_idx_per_10, -1, -1), new_syncmap.unsqueeze(0).expand(current_state_idx_per_10, -1, -1))
            
        return new_syncmap
    

    def cluster(self):
        self.organized= True
        # self.labels= DBSCAN(eps=3, min_samples=2).fit_predict(self.syncmap)
        self.labels= DBSCAN(eps=1, min_samples=2).fit_predict(self.syncmap.cpu().numpy())

        return self.labels

    def activate(self, x):
        '''
        Return the label of the index with maximum input value
        '''

        if self.organized == False:
            print("Activating a non-organized SyncMap")
            return
        
        #maximum output
        max_index= np.argmax(x)

        return self.labels[max_index]
    

def data_preprocessing(data):
    '''
    data: np.array, shape = (N, D), N is the number of samples, D is the number of variables
    '''
    data = data > 0.1
    mask = []
    for idx, item in enumerate(data):
        if item.sum() > 1:
            mask.append(idx)
    return data[mask]
