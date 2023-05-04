from copy import deepcopy
from typing import Tuple, Optional, List, Dict
from easydict import EasyDict
from ditk import logging
import pickle
import os
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim

from ding.utils import REWARD_MODEL_REGISTRY
from ding.utils import SequenceType
from ding.model.common import FCEncoder
from ding.utils import build_logger
from ding.utils.data import default_collate

from .base_reward_model import BaseRewardModel
from .reword_model_utils import collect_states
from .network import TREXNetwork


@REWARD_MODEL_REGISTRY.register('trex')
class TrexRewardModel(BaseRewardModel):
    """
    Overview:
        The Trex reward model class (https://arxiv.org/pdf/1904.06387.pdf)
    Interface:
        ``estimate``, ``train``, ``load_expert_data``, ``collect_data``, ``clear_date``, \
            ``__init__``, ``_train``,
    Config:
        == ====================  ======   =============  ============================================  =============
        ID Symbol                Type     Default Value  Description                                   Other(Shape)
        == ====================  ======   =============  ============================================  =============
        1  ``type``              str       trex          | Reward model register name, refer           |
                                                         | to registry ``REWARD_MODEL_REGISTRY``       |
        3  | ``learning_rate``   float     0.00001       | learning rate for optimizer                 |
        4  | ``update_per_``     int       100           | Number of updates per collect               |
           | ``collect``                                 |                                             |
        5  | ``num_trajs``       int       0             | Number of downsampled full trajectories     |
        6  | ``num_snippets``    int       6000          | Number of short subtrajectories to sample   |
        == ====================  ======   =============  ============================================  =============
    """
    config = dict(
        # (str) Reward model register name, refer to registry ``REWARD_MODEL_REGISTRY``.
        type='trex',
        # (float) The step size of gradient descent.
        learning_rate=1e-5,
        # (int) How many updates(iterations) to train after collector's one collection.
        # Bigger "update_per_collect" means bigger off-policy.
        # collect data -> update policy-> collect data -> ...
        update_per_collect=100,
        # (int) Number of downsampled full trajectories.
        num_trajs=0,
        # (int) Number of short subtrajectories to sample.
        num_snippets=6000,
    )

    def __init__(self, config: EasyDict, device: str, tb_logger: 'SummaryWriter') -> None:  # noqa
        """
        Overview:
            Initialize ``self.`` See ``help(type(self))`` for accurate signature.
        Arguments:
            - cfg (:obj:`EasyDict`): Training config
            - device (:obj:`str`): Device usage, i.e. "cpu" or "cuda"
            - tb_logger (:obj:`SummaryWriter`): Logger, defaultly set as 'SummaryWriter' for model summary
        """
        super(TrexRewardModel, self).__init__()
        self.cfg = config
        assert device in ["cpu", "cuda"] or "cuda" in device
        self.device = device
        self.tb_logger = tb_logger
        kernel_size = config.kernel_size if 'kernel_size' in config else None
        stride = config.stride if 'stride' in config else None
        self.reward_model = TREXNetwork(self.cfg.obs_shape, config.hidden_size_list, kernel_size, stride)
        self.reward_model.to(self.device)
        self.pre_expert_data = []
        self.train_data = []
        self.expert_data_loader = None
        self.opt = optim.Adam(self.reward_model.parameters(), config.learning_rate)
        self.train_iter = 0
        self.estimate_iter = 0
        self.learning_returns = []
        self.training_obs = []
        self.training_labels = []
        self.num_trajs = self.cfg.num_trajs
        self.num_snippets = self.cfg.num_snippets
        # minimum number of short subtrajectories to sample
        self.min_snippet_length = config.min_snippet_length
        # maximum number of short subtrajectories to sample
        self.max_snippet_length = config.max_snippet_length
        self.l1_reg = 0
        self.data_for_save = {}
        self._logger, self._tb_logger = build_logger(
            path='./{}/log/{}'.format(self.cfg.exp_name, 'trex_reward_model'), name='trex_reward_model'
        )
        self.load_expert_data()

    def load_expert_data(self) -> None:
        """
        Overview:
            Getting the expert data.
        Effects:
            This is a side effect function which updates the expert data attribute \
                (i.e. ``self.expert_data``) with ``fn:concat_state_action_pairs``
        """
        with open(os.path.join(self.cfg.exp_name, 'episodes_data.pkl'), 'rb') as f:
            self.pre_expert_data = pickle.load(f)
        with open(os.path.join(self.cfg.exp_name, 'learning_returns.pkl'), 'rb') as f:
            self.learning_returns = pickle.load(f)

        self.create_training_data()
        logging.info("num_training_obs: {}".format(len(self.training_obs)))
        logging.info("num_labels: {}".format(len(self.training_labels)))

    def create_training_data(self):
        num_trajs = self.num_trajs
        num_snippets = self.num_snippets
        min_snippet_length = self.min_snippet_length
        max_snippet_length = self.max_snippet_length

        demo_lengths = []
        for i in range(len(self.pre_expert_data)):
            demo_lengths.append([len(d) for d in self.pre_expert_data[i]])

        logging.info("demo_lengths: {}".format(demo_lengths))
        max_snippet_length = min(np.min(demo_lengths), max_snippet_length)
        logging.info("min snippet length: {}".format(min_snippet_length))
        logging.info("max snippet length: {}".format(max_snippet_length))

        # collect training data
        max_traj_length = 0
        num_bins = len(self.pre_expert_data)
        assert num_bins >= 2

        # add full trajs (for use on Enduro)
        si = np.random.randint(6, size=num_trajs)
        sj = np.random.randint(6, size=num_trajs)
        step = np.random.randint(3, 7, size=num_trajs)
        for n in range(num_trajs):
            # pick two random demonstrations
            bi, bj = np.random.choice(num_bins, size=(2, ), replace=False)
            ti = np.random.choice(len(self.pre_expert_data[bi]))
            tj = np.random.choice(len(self.pre_expert_data[bj]))
            # create random partial trajs by finding random start frame and random skip frame
            traj_i = self.pre_expert_data[bi][ti][si[n]::step[n]]  # slice(start,stop,step)
            traj_j = self.pre_expert_data[bj][tj][sj[n]::step[n]]

            label = int(bi <= bj)

            self.training_obs.append((traj_i, traj_j))
            self.training_labels.append(label)
            max_traj_length = max(max_traj_length, len(traj_i), len(traj_j))

        # fixed size snippets with progress prior
        rand_length = np.random.randint(min_snippet_length, max_snippet_length, size=num_snippets)
        for n in range(num_snippets):
            # pick two random demonstrations
            bi, bj = np.random.choice(num_bins, size=(2, ), replace=False)
            ti = np.random.choice(len(self.pre_expert_data[bi]))
            tj = np.random.choice(len(self.pre_expert_data[bj]))
            # create random snippets
            # find min length of both demos to ensure we can pick a demo no earlier
            # than that chosen in worse preferred demo
            min_length = min(len(self.pre_expert_data[bi][ti]), len(self.pre_expert_data[bj][tj]))
            if bi < bj:  # pick tj snippet to be later than ti
                ti_start = np.random.randint(min_length - rand_length[n] + 1)
                # print(ti_start, len(demonstrations[tj]))
                tj_start = np.random.randint(ti_start, len(self.pre_expert_data[bj][tj]) - rand_length[n] + 1)
            else:  # ti is better so pick later snippet in ti
                tj_start = np.random.randint(min_length - rand_length[n] + 1)
                # print(tj_start, len(demonstrations[ti]))
                ti_start = np.random.randint(tj_start, len(self.pre_expert_data[bi][ti]) - rand_length[n] + 1)
            # skip everyother framestack to reduce size
            traj_i = self.pre_expert_data[bi][ti][ti_start:ti_start + rand_length[n]:2]
            traj_j = self.pre_expert_data[bj][tj][tj_start:tj_start + rand_length[n]:2]

            max_traj_length = max(max_traj_length, len(traj_i), len(traj_j))
            label = int(bi <= bj)
            self.training_obs.append((traj_i, traj_j))
            self.training_labels.append(label)
        logging.info(("maximum traj length: {}".format(max_traj_length)))
        return self.training_obs, self.training_labels

    def _train(self, training_obs: Tuple, training_labels: Tuple) -> float:
        # check if gpu available
        device = self.device  # torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        # Assume that we are on a CUDA machine, then this should print a CUDA device:
        logging.info("device: {}".format(device))
        cum_loss = 0.0
        for i in range(len(training_labels)):

            # traj_i, traj_j has the same length, however, they change as i increases
            traj_i, traj_j = training_obs[i]  # traj_i is a list of array generated by env.step
            traj_i = np.array(traj_i)
            traj_j = np.array(traj_j)
            traj_i = torch.from_numpy(traj_i).float().to(device)
            traj_j = torch.from_numpy(traj_j).float().to(device)

            # training_labels[i] is a boolean integer: 0 or 1
            labels = torch.tensor([training_labels[i]]).to(device)

            # forward + backward + zero out gradient + optimize
            loss = self.reward_model.learn(traj_i, traj_j, labels)
            self.opt.zero_grad()
            loss.backward()
            self.opt.step()

            # print stats to see if learning
            item_loss = loss.item()
            cum_loss += item_loss
            return cum_loss
        # if not os.path.exists(os.path.join(self.cfg.exp_name, 'ckpt_reward_model')):
        #     os.makedirs(os.path.join(self.cfg.exp_name, 'ckpt_reward_model'))
        # torch.save(self.reward_model.state_dict(), os.path.join(self.cfg.exp_name,
        # 'ckpt_reward_model/latest.pth.tar'))
        # logging.info("finished training")

    def train(self):
        # check if gpu available
        device = self.device  # torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        # Assume that we are on a CUDA machine, then this should print a CUDA device:
        logging.info("device: {}".format(device))
        training_inputs, training_outputs = self.training_obs, self.training_labels

        cum_loss = 0.0
        training_data = list(zip(training_inputs, training_outputs))
        for epoch in range(self.cfg.update_per_collect):
            np.random.shuffle(training_data)
            training_obs, training_labels = zip(*training_data)
            cum_loss = self._train(training_obs, training_labels)
            self.train_iter += 1
            logging.info("[epoch {}] loss {}".format(epoch, cum_loss))
            self.tb_logger.add_scalar("trex_reward/train_loss_iteration", cum_loss, self.train_iter)
        # print out predicted cumulative returns and actual returns
        sorted_returns = sorted(self.learning_returns, key=lambda s: s[0])
        demonstrations = [
            x for _, x in sorted(zip(self.learning_returns, self.pre_expert_data), key=lambda pair: pair[0][0])
        ]
        with torch.no_grad():
            pred_returns = [self.predict_traj_return(self.reward_model, traj[0]) for traj in demonstrations]
        for i, p in enumerate(pred_returns):
            logging.info("{} {} {}".format(i, p, sorted_returns[i][0]))
        info = {
            "demo_length": [len(d[0]) for d in self.pre_expert_data],
            "min_snippet_length": self.min_snippet_length,
            "max_snippet_length": min(np.min([len(d[0]) for d in self.pre_expert_data]), self.max_snippet_length),
            "len_num_training_obs": len(self.training_obs),
            "lem_num_labels": len(self.training_labels),
            "accuracy": self.calc_accuracy(self.reward_model, self.training_obs, self.training_labels),
        }
        logging.info("accuracy and comparison:\n{}".format('\n'.join(['{}: {}'.format(k, v) for k, v in info.items()])))

    def predict_traj_return(self, net, traj):
        device = self.device
        # torch.set_printoptions(precision=20)
        # torch.use_deterministic_algorithms(True)
        with torch.no_grad():
            rewards_from_obs = net.forward(torch.from_numpy(np.array(traj)).float().to(device)).squeeze().tolist()
            # rewards_from_obs1 = net.cum_return(torch.from_numpy(np.array([traj[0]])).float().to(device))[0].item()
            # different precision
        return sum(rewards_from_obs)  # rewards_from_obs is a list of floats

    def calc_accuracy(self, reward_network, training_inputs, training_outputs):
        device = self.device
        loss_criterion = nn.CrossEntropyLoss()
        num_correct = 0.
        with torch.no_grad():
            for i in range(len(training_inputs)):
                label = training_outputs[i]
                traj_i, traj_j = training_inputs[i]
                traj_i = np.array(traj_i)
                traj_j = np.array(traj_j)
                traj_i = torch.from_numpy(traj_i).float().to(device)
                traj_j = torch.from_numpy(traj_j).float().to(device)

                #forward to get logits
                outputs, abs_return = reward_network.get_outputs_abs_reward(traj_i, traj_j)
                _, pred_label = torch.max(outputs, 0)
                if pred_label.item() == label:
                    num_correct += 1.
        return num_correct / len(training_inputs)

    def pred_data(self, data):
        obs = [default_collate(data[i])['obs'] for i in range(len(data))]
        res = [torch.sum(default_collate(data[i])['reward']).item() for i in range(len(data))]
        pred_returns = [self.predict_traj_return(self.reward_model, obs[i]) for i in range(len(obs))]
        return {'real': res, 'pred': pred_returns}

    def estimate(self, data: list) -> List[Dict]:
        """
        Overview:
            Estimate reward by rewriting the reward key in each row of the data.
        Arguments:
            - data (:obj:`list`): the list of data used for estimation, with at least \
                 ``obs`` and ``action`` keys.
        Effects:
            - This is a side effect function which updates the reward values in place.
        """
        # NOTE: deepcopy reward part of data is very important,
        # otherwise the reward of data in the replay buffer will be incorrectly modified.
        train_data_augmented = self.reward_deepcopy(data)

        res = collect_states(train_data_augmented)
        res = torch.stack(res).to(self.device)
        with torch.no_grad():
            sum_rewards = self.reward_model.forward(res)
            self.tb_logger.add_scalar("trex_reward/estimate_reward_mean", sum_rewards.mean().item(), self.train_iter)
            self.tb_logger.add_scalar("trex_reward/estimate_reward_std", sum_rewards.std().item(), self.train_iter)
            self.tb_logger.add_scalar("trex_reward/estimate_reward_max", sum_rewards.max().item(), self.train_iter)
            self.tb_logger.add_scalar("trex_reward/estimate_reward_min", sum_rewards.min().item(), self.train_iter)

        for item, rew in zip(train_data_augmented, sum_rewards):  # TODO optimise this loop as well ?
            item['reward'] = rew

        return train_data_augmented

    def collect_data(self, data: list) -> None:
        """
        Overview:
            Collecting training data formatted by  ``fn:concat_state_action_pairs``.
        Arguments:
            - data (:obj:`Any`): Raw training data (e.g. some form of states, actions, obs, etc)
        Effects:
            - This is a side effect function which updates the data attribute in ``self``
        """
        pass

    def clear_data(self, iter: int) -> None:
        """
        Overview:
            Clearing training data. \
            This is a side effect function which clears the data attribute in ``self``
        """
        if hasattr(self.cfg, 'clear_buffer_per_iters') and iter % self.cfg.clear_buffer_per_iters == 0:
            self.training_obs.clear()
            self.training_labels.clear()
