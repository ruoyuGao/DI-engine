from typing import Any, List, Union, Optional
import time
import gym
import numpy as np
from easydict import EasyDict
from ding.envs import BaseEnv, BaseEnvTimestep
from ding.torch_utils import to_ndarray, to_list
from ding.utils import ENV_REGISTRY
from ding.envs import ObsPlusPrevActRewWrapper


@ENV_REGISTRY.register('gym_mobile')
class MobileEnv(BaseEnv):

    def __init__(self, cfg: dict = {}) -> None:
        self._cfg = cfg
        self._init_flag = False
        self._replay_path = None
        self._observation_space = gym.spaces.Box(low=0, high=255, shape=(80, 80, 6), dtype=np.float32)
        self._action_space = gym.spaces.Discrete(6)
        self._reward_space = gym.spaces.Box(low=0, high=20, shape=(1,), dtype=np.float32)
        self._action_space.seed(0)  # default seed

    def reset(self) -> np.ndarray:
        if not self._init_flag:
            self._env = gym.make('Boxworld-v0')
            if self._replay_path is not None:
                self._env = gym.wrappers.RecordVideo(
                    self._env,
                    video_folder=self._replay_path,
                    episode_trigger=lambda episode_id: True,
                    name_prefix='rl-video-{}'.format(id(self))
                )
            self._init_flag = True
        self._observation_space = self._env.observation_space
        self._eval_episode_return = 0
        obs = self._env.reset()
        obs = to_ndarray(obs)
        return obs

    def close(self) -> None:
        if self._init_flag:
            self._env.close()
        self._init_flag = False

    def seed(self, seed: int, dynamic_seed: bool = True) -> None:
        self._seed = seed
        self._dynamic_seed = dynamic_seed
        np.random.seed(self._seed)

    def step(self, action: Union[int, np.ndarray]) -> BaseEnvTimestep:
        if isinstance(action, np.ndarray) and action.shape == (1, ):
            action = action.squeeze()  # 0-dim array
        obs, rew, done, info = self._env.step(action)
        self._eval_episode_return += rew
        if done:
            info['eval_episode_return'] = self._eval_episode_return
        obs = to_ndarray(obs)
        rew = to_ndarray([rew]).astype(np.float32)  # wrapped to be transfered to a array with shape (1,)
        return BaseEnvTimestep(obs, rew, done, info)

    def enable_save_replay(self, replay_path: Optional[str] = None) -> None:
        if replay_path is None:
            replay_path = './video'
        self._replay_path = replay_path

    def random_action(self) -> np.ndarray:
        random_action = self.action_space.sample()
        random_action = to_ndarray([random_action], dtype=np.int64)
        return random_action

    @property
    def observation_space(self) -> gym.spaces.Space:
        return self._observation_space

    @property
    def action_space(self) -> gym.spaces.Space:
        return self._action_space

    @property
    def reward_space(self) -> gym.spaces.Space:
        return self._reward_space

    def __repr__(self) -> str:
        return "DI-engine gym_mobile Env"
