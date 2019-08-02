# -*- coding: utf-8 -*-
"""
Created on Thu Jul 18 20:48:16 CST 2019

@author: chenzhen
"""
import time
import threading

from core import (Variable, get_trainable_variables_from_graph,
                  update_node_value_in_graph)
from core.graph import default_graph
from dist import allreduce, ps
from trainer import Trainer


class DistTrainerParameterServer(Trainer):
    def __init__(self, *args, **kargs):
        Trainer.__init__(self, *args, **kargs)
        cluster_conf = kargs['cluster_conf']
        ps_host = cluster_conf['ps'][0]
        self.ps_client = ps.ParameterServiceClient(ps_host)

    def _variable_weights_init(self):
        '''
        多个worker通过ps保证权值变量的一致
        '''
        var_weights_dict = dict()
        for node in default_graph.nodes:
            if isinstance(node, Variable) and node.trainable:
                var_weights_dict[node.name] = node.value
        duplicated_var_weights_dict = self.ps_client.variable_weights_init(
            var_weights_dict)
        for var_name, weights in duplicated_var_weights_dict.items():
            update_node_value_in_graph(var_name, weights)

    def _optimizer_update(self):
        # 把当前梯度push到ps上。此操作可能被block，直到所有节点都pull完成
        acc_gradient = self.optimizer.acc_gradient
        self.ps_client.push_gradients(
            acc_gradient, self.optimizer.acc_no)
        # 从ps把所有节点的平均梯度pull回来。此操作可能被block直到所有节点都push完成
        node_gradients_dict = self.ps_client.pull_gradients()
        # 使用平均梯度，更新本地变量
        self.optimizer.update(node_gradients_dict)


class DistTrainerRingAllReduce(Trainer):
    '''
    Ring All-Reduce模式的分布式训练
    '''

    def __init__(self, *args, **kargs):
        Trainer.__init__(self, *args, **kargs)
        self.cluster_conf = kargs['cluster_conf']
        self.worker_index = kargs['worker_index']

        self.workers = self.cluster_conf['workers']
        self.worker_num = len(self.workers)
        self.host = self.workers[self.worker_index]

        self.step = self.worker_num - 1
        self.target_host = self.workers[(
            self.worker_index + 1) % self.worker_num]

        self.cur_partion_index = self.worker_index
        self.partition = []
        # 获取所有可训练节点，即所有需要更新的权值变量
        self.variables = get_trainable_variables_from_graph()
        # 根据worker的总数量，对即将更新的权值变量列表进行等长切分
        self._partition_variables()

        # 用于控制梯度的发送和接收
        self.is_recieved = False
        self.recieved_gradients = None
        self.recieved_acc_no = None
        self.cond = threading.Condition()

        # 创建本节点的梯度接收服务
        allreduce.RingAllReduceServer(
            self.host, self.worker_index, self._scatter_callback, self._gather_callback).serve()
        # 创建连接目标节点的梯度发送client
        self.client = allreduce.RingAllReduceClient(self.target_host)

    def _partition_variables(self):
        '''
        根据worker的总数量，对即将更新的权值变量列表进行等长切分
        '''
        var_num = len(self.variables)
        part_length = int(var_num / self.worker_num)
        assert part_length > 0
        start = 0
        end = start + part_length
        for i in range(self.worker_num - 1):
            self.partition.append((start, end))
            start = end
            end = start + part_length
        self.partition.append((start, var_num))

    def _get_gradients_partition(self):
        '''
        获取下一个梯度切片
        '''
        start, end = self.partition[self.cur_partion_index]
        part_variables = self.variables[start:end]
        self.cur_partion_index = (
            self.cur_partion_index + self.step) % self.worker_num
        part_gradients = dict()
        for var in part_variables:
            part_gradients[var] = self.optimizer.acc_gradient[var]
        return part_gradients

    def _scatter_callback(self, node_gradients_dict, acc_no):
        '''
        Scatter 阶段的回调函数，接收上一个节点发送过来的梯度和样本数
        '''
        if self.cond.acquire():
            while self.is_recieved:
                self.cond.wait()

            self.recieved_gradients = node_gradients_dict
            self.recieved_acc_no = acc_no
            self.is_recieved = True
            # 通知主流程，把接收到的梯度更新到优化器
            self.cond.notify_all()
            self.cond.release()
        else:
            self.cond.wait()

    def _gather_callback(self, node_gradients_dict):
        '''
        All-gather 节点的回调函数，接收上一个节点发送过来的梯度
        '''
        if self.cond.acquire():
            while self.is_recieved:
                self.cond.wait()

            self.recieved_gradients = node_gradients_dict
            self.is_recieved = True
            # 通知主流程，把接收到的梯度更新到优化器
            self.cond.notify_all()
            self.cond.release()
        else:
            self.cond.wait()

    def _wait_for_recieve(self, stage):
        '''
        等待梯度接收并使用接收到的梯度，更新到优化器中
        '''
        if self.cond.acquire():
            while not self.is_recieved:
                self.cond.wait()
            # 如果是scatter阶段，梯度累加更新，同时累加样本数
            if stage == 'scatter':
                self.optimizer.apply_gradients(
                    self.recieved_gradients,  summarize=True, acc_no=self.recieved_acc_no)
            # 如果是all-gather节点，梯度覆盖更新，样本数保持不变
            else:
                self.optimizer.apply_gradients(
                    self.recieved_gradients, summarize=False, acc_no=self.optimizer.acc_no)
            self.is_recieved = False
            # 梯度已被更新，通知接收流程可以继续接收新的梯度
            self.cond.notify_all()
            self.cond.release()
        else:
            self.cond.wait()

    def _optimizer_update(self):
        # N-1 次的scatter操作，把本节点的梯度切片发送给下一个节点
        # 同时接收上一个节点发送过来的梯度并累加更新到当前节点的对应切片
        for scatter_index in range(self.step):
            gradients_part = self._get_gradients_partition()
            cur_acc_no = self.optimizer.acc_no if scatter_index == 0 else self.recieved_acc_no
            self.client.send(gradients_part, cur_acc_no, 'scatter')
            self._wait_for_recieve('scatter')
        # N-1次的all-gather操作，把本节点的梯度切片发送给下一个节点
        # 同时接收上一个节点发送过来的梯度并替换更新到当前节点的对应切片
        for gather_index in range(self.step):
            gradients_part = self._get_gradients_partition()
            self.client.send(gradients_part, 0, 'gather')
            self._wait_for_recieve('gather')

        self.optimizer.update()