import queue
import time
from multiprocessing import Queue, Process

import cv2  # pytype:disable=import-error
import numpy as np
from joblib import Parallel, delayed
import warnings
from itertools import cycle, islice


from stable_baselines import logger


class ExpertDataset(object):
    """
    Dataset for using behavior cloning or GAIL.

    The structure of the expert dataset is a dict, saved as an ".npz" archive.
    The dictionary contains the keys 'actions', 'episode_returns', 'rewards', 'obs' and 'episode_starts'.
    The corresponding values have data concatenated across episode: the first axis is the timestep,
    the remaining axes index into the data. In case of images, 'obs' contains the relative path to
    the images, to enable space saving from image compression.

    :param expert_path: (str) The path to trajectory data (.npz file). Mutually exclusive with traj_data.
    :param traj_data: (dict) Trajectory data, in format described above. Mutually exclusive with expert_path.
    :param train_fraction: (float) the train validation split (0 to 1)
        for pre-training using behavior cloning (BC)
    :param batch_size: (int) the minibatch size for behavior cloning
    :param traj_limitation: (int) the number of trajectory to use (if -1, load all)
    :param randomize: (bool) if the dataset should be shuffled, this will be overwritten to False if LSTM is True
    :param verbose: (int) Verbosity
    :param sequential_preprocessing: (bool) Do not use subprocess to preprocess
        the data (slower but use less memory for the CI)
    :param LSTM: (bool) If model to pretrain uses a recurrent policy.
    :param envs_per_batch: (int) Only used if LSTM is True. Number of envs that are processed per batch.
    """

    def __init__(self, expert_path=None, traj_data=None, train_fraction=0.7,
                 batch_size=64, traj_limitation=-1, randomize=True, verbose=1,
                 sequential_preprocessing=False, LSTM=False, envs_per_batch=1):
        if traj_data is not None and expert_path is not None:
            raise ValueError("Cannot specify both 'traj_data' and 'expert_path'")
        if traj_data is None and expert_path is None:
            raise ValueError("Must specify one of 'traj_data' or 'expert_path'")
        if traj_data is None:
            traj_data = np.load(expert_path, allow_pickle=True)

        if verbose > 0:
            for key, val in traj_data.items():
                print(key, val.shape)

        envs_per_batch = int(envs_per_batch)
        use_batch_size = batch_size * envs_per_batch

        # Array of bool where episode_starts[i] = True for each new episode
        episode_starts = traj_data['episode_starts']

        traj_limit_idx = len(traj_data['observations'])

        if traj_limitation > 0:
            n_episodes = 0
            # Retrieve the index corresponding
            # to the traj_limitation trajectory
            for idx, episode_start in enumerate(episode_starts):
                n_episodes += int(episode_start)
                if n_episodes == (traj_limitation + 1):
                    traj_limit_idx = idx - 1

        observations = traj_data['observations'][:traj_limit_idx]
        actions = traj_data['actions'][:traj_limit_idx]

        mask = episode_starts[:traj_limit_idx]

        start_index_list = []
        if LSTM:
            for idx, episode_start in enumerate(mask):
                #print(idx, episode_start)
                if episode_start:
                    #print('Blal')
                    start_index_list.append(idx)

            start_index_list += [traj_limit_idx]
        #print(start_index_list)

        # obs, actions: shape (N * L, ) + S
        # where N = # episodes, L = episode length
        # and S is the environment observation/action space.
        # S = (1, ) for discrete space
        # Flatten to (N * L, prod(S))
        #if len(observations.shape) > 2:
        #    observations = np.reshape(observations, [-1, np.prod(observations.shape[1:])])

        actions = actions.reshape(-1,1)

        if len(actions.shape) > 2:
            actions = np.reshape(actions, [-1, np.prod(actions.shape[1:])])

        if len(mask.shape) > 2:
            mask = np.reshape(mask, [-1, np.prod(mask.shape[1:])])

        if LSTM:
            # CReate indices list and split them per episode
            indices = np.arange(start=0, stop=len(observations)).astype(np.int64)
            #print(indices)
            split_indices = [indices[start_index_list[i]:start_index_list[i+1]].tolist() for i in range(0, len(start_index_list)-1)]
            #print(split_indices)

            # create list with episode lengths
            len_list = [len(s_i) for s_i in split_indices]
            #print(len_list)

            assert len(len_list) >= envs_per_batch, "Not enough saved episodes for this number of workers and nminibatches."

            # sort episode pos by length
            sort_buffer = np.argsort(len_list).tolist()[::-1]
            stack_indices = []
            for i in range(envs_per_batch):
                stack_indices.append(split_indices[sort_buffer[0]])
                sort_buffer.pop(0)

            # add next episode to the smalles stack
            for s_b in sort_buffer:
                currend_stackt_indices_len = [len(st_i) for st_i in stack_indices]
                smalest_stackt_indices_pos = np.argmin(currend_stackt_indices_len)
                stack_indices[smalest_stackt_indices_pos] += split_indices[s_b]

            pre_cycle_len = [len(st_i) for st_i in stack_indices]
            max_len = max(pre_cycle_len)
            min_len = min(pre_cycle_len)

            mod_max_len = max_len % batch_size
            final_stack_len = max_len + (batch_size - mod_max_len)

            split_point = int(train_fraction * final_stack_len * envs_per_batch)
            split_point = split_point - (split_point % use_batch_size)

            if mod_max_len > (min_len - (final_stack_len * envs_per_batch - split_point)) > 0:
                warnings.warn('The Episode are divide to unequal, your validation set will '
                              'get polluted with training data.')

            cycle_indices = [list(islice(cycle(st_i), None, final_stack_len)) for st_i in stack_indices]
            cycle_indices = [cycle_indices[i][pre_cycle_len[i]:] + cycle_indices[i][:pre_cycle_len[i]]\
                             for i in range(len(pre_cycle_len))]

            # Flatten the stack cycle list to a single list.
            indices = []
            for i in range(0, len(cycle_indices[0]), batch_size):
                for c_i in cycle_indices:
                    indices += c_i[i:i+batch_size]

            del split_indices, len_list, sort_buffer, stack_indices, max_len, mod_max_len, final_stack_len, cycle_indices

            train_indices = indices[:split_point]
            val_indices = indices[split_point:]

            self.randomize = False

        else:

            indices = np.random.permutation(len(observations)).astype(np.int64)

            # Train/Validation split when using behavior cloning
            train_indices = indices[:int(train_fraction * len(indices))]
            val_indices = indices[int(train_fraction * len(indices)):]

            self.randomize = randomize

        assert len(train_indices) > 0, "No sample for the training set"
        assert len(val_indices) > 0, "No sample for the validation set"

        self.observations = observations
        self.actions = actions
        self.mask = mask

        self.returns = traj_data['episode_returns'][:traj_limit_idx]
        self.avg_ret = sum(self.returns) / len(self.returns)
        self.std_ret = np.std(np.array(self.returns))
        self.verbose = verbose

        assert len(self.observations) == len(self.actions), "The number of actions and observations differ " \
                                                            "please check your expert dataset"
        self.num_traj = min(traj_limitation, np.sum(episode_starts))
        self.num_transition = len(self.observations)
        self.sequential_preprocessing = sequential_preprocessing

        self.dataloader = None
        # self.train_loader = DataLoader(train_indices, self.observations, self.actions, batch_size,
        #                                shuffle=self.randomize, start_process=False,
        #                                sequential=sequential_preprocessing)
        self.train_loader = DataLoader(train_indices, self.observations, self.actions, self.mask, use_batch_size,
                                       shuffle=self.randomize, start_process=False,
                                       sequential=sequential_preprocessing, partial_minibatch= not LSTM)
        self.val_loader = DataLoader(val_indices, self.observations, self.actions, self.mask, use_batch_size,
                                     shuffle=self.randomize, start_process=False,
                                     sequential=sequential_preprocessing, partial_minibatch= not LSTM)
        # self.val_loader = DataLoader(val_indices, self.observations, self.actions, batch_size,
        #                              shuffle=self.randomize, start_process=False,
        #                              sequential=sequential_preprocessing)

        if self.verbose >= 1:
            self.log_info()

    def init_dataloader(self, batch_size):
        """
        Initialize the dataloader used by GAIL.

        :param batch_size: (int)
        """
        indices = np.random.permutation(len(self.observations)).astype(np.int64)
        self.dataloader = DataLoader(indices, self.observations, self.actions, self.mask, batch_size,
                                     shuffle=self.randomize, start_process=False,
                                     sequential=self.sequential_preprocessing)

    def __del__(self):
        del self.dataloader, self.train_loader, self.val_loader

    def prepare_pickling(self):
        """
        Exit processes in order to pickle the dataset.
        """
        self.dataloader, self.train_loader, self.val_loader = None, None, None

    def log_info(self):
        """
        Log the information of the dataset.
        """
        logger.log("Total trajectories: {}".format(self.num_traj))
        logger.log("Total transitions: {}".format(self.num_transition))
        logger.log("Average returns: {}".format(self.avg_ret))
        logger.log("Std for returns: {}".format(self.std_ret))

    def get_next_batch(self, split=None):
        """
        Get the batch from the dataset.

        :param split: (str) the type of data split (can be None, 'train', 'val')
        :return: (np.ndarray, np.ndarray) inputs and labels
        """
        dataloader = {
            None: self.dataloader,
            'train': self.train_loader,
            'val': self.val_loader
        }[split]

        if dataloader.process is None:
            dataloader.start_process()
        try:
            return next(dataloader)
        except StopIteration:
            dataloader = iter(dataloader)
            return next(dataloader)

    def plot(self):
        """
        Show histogram plotting of the episode returns
        """
        # Isolate dependency since it is only used for plotting and also since
        # different matplotlib backends have further dependencies themselves.
        import matplotlib.pyplot as plt
        plt.hist(self.returns)
        plt.show()


class DataLoader(object):
    """
    A custom dataloader to preprocessing observations (including images)
    and feed them to the network.

    Original code for the dataloader from https://github.com/araffin/robotics-rl-srl
    (MIT licence)
    Authors: Antonin Raffin, René Traoré, Ashley Hill

    :param indices: ([int]) list of observations indices
    :param observations: (np.ndarray) observations or images path
    :param actions: (np.ndarray) actions
    :param batch_size: (int) Number of samples per minibatch
    :param n_workers: (int) number of preprocessing worker (for loading the images)
    :param infinite_loop: (bool) whether to have an iterator that can be reset
    :param max_queue_len: (int) Max number of minibatches that can be preprocessed at the same time
    :param shuffle: (bool) Shuffle the minibatch after each epoch
    :param start_process: (bool) Start the preprocessing process (default: True)
    :param backend: (str) joblib backend (one of 'multiprocessing', 'sequential', 'threading'
        or 'loky' in newest versions)
    :param sequential: (bool) Do not use subprocess to preprocess the data
        (slower but use less memory for the CI)
    :param partial_minibatch: (bool) Allow partial minibatches (minibatches with a number of element
        lesser than the batch_size)
    """

    def __init__(self, indices, observations, actions, mask, batch_size, n_workers=1,
                 infinite_loop=True, max_queue_len=1, shuffle=False,
                 start_process=True, backend='threading', sequential=False, partial_minibatch=True):
        super(DataLoader, self).__init__()
        self.n_workers = n_workers
        self.infinite_loop = infinite_loop
        self.indices = indices
        self.original_indices = indices.copy()
        self.n_minibatches = len(indices) // batch_size
        # Add a partial minibatch, for instance
        # when there is not enough samples
        if partial_minibatch and len(indices) % batch_size > 0:
            self.n_minibatches += 1
        self.batch_size = batch_size
        self.observations = observations
        self.actions = actions
        self.mask = mask
        self.shuffle = shuffle
        self.queue = Queue(max_queue_len)
        self.process = None
        self.load_images = False #isinstance(observations[0], str)
        self.backend = backend
        self.sequential = sequential
        self.start_idx = 0
        if start_process:
            self.start_process()

    def start_process(self):
        """Start preprocessing process"""
        # Skip if in sequential mode
        if self.sequential:
            return
        self.process = Process(target=self._run)
        # Make it a deamon, so it will be deleted at the same time
        # of the main process
        self.process.daemon = True
        self.process.start()

    @property
    def _minibatch_indices(self):
        """
        Current minibatch indices given the current pointer
        (start_idx) and the minibatch size
        :return: (np.ndarray) 1D array of indices
        """
        return self.indices[self.start_idx:self.start_idx + self.batch_size]

    def sequential_next(self):
        """
        Sequential version of the pre-processing.
        """
        if self.start_idx >= len(self.indices):
            raise StopIteration

        if self.start_idx == 0:
            if self.shuffle:
                # Shuffle indices
                np.random.shuffle(self.indices)

        obs = self.observations[self._minibatch_indices]
        if self.load_images:
            obs = np.concatenate([self._make_batch_element(image_path) for image_path in obs],
                                 axis=0)

        actions = self.actions[self._minibatch_indices]
        mask = self.mask[self._minibatch_indices]

        self.start_idx += self.batch_size
        return obs, actions, mask

    def _run(self):
        start = True
        with Parallel(n_jobs=self.n_workers, batch_size="auto", backend=self.backend) as parallel:
            while start or self.infinite_loop:
                start = False

                if self.shuffle:
                    np.random.shuffle(self.indices)

                for minibatch_idx in range(self.n_minibatches):

                    self.start_idx = minibatch_idx * self.batch_size

                    obs = self.observations[self._minibatch_indices]
                    if self.load_images:
                        if self.n_workers <= 1:
                            obs = [self._make_batch_element(image_path)
                                   for image_path in obs]

                        else:
                            obs = parallel(delayed(self._make_batch_element)(image_path)
                                           for image_path in obs)

                        obs = np.concatenate(obs, axis=0)

                    actions = self.actions[self._minibatch_indices]
                    mask = self.mask[self._minibatch_indices]

                    #self.queue.put((obs, actions))
                    self.queue.put((obs, actions, mask))

                    # Free memory
                    del obs

                self.queue.put(None)

    @classmethod
    def _make_batch_element(cls, image_path):
        """
        Process one element.

        :param image_path: (str) path to an image
        :return: (np.ndarray)
        """
        # cv2.IMREAD_UNCHANGED is needed to load
        # grey and RGBa images
        image = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
        # Grey image
        if len(image.shape) == 2:
            image = image[:, :, np.newaxis]

        if image is None:
            raise ValueError("Tried to load {}, but it was not found".format(image_path))
        # Convert from BGR to RGB
        if image.shape[-1] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = image.reshape((1,) + image.shape)
        return image

    def __len__(self):
        return self.n_minibatches

    def __iter__(self):
        self.start_idx = 0
        self.indices = self.original_indices.copy()
        return self

    def __next__(self):
        if self.sequential:
            return self.sequential_next()

        if self.process is None:
            raise ValueError("You must call .start_process() before using the dataloader")
        while True:
            try:
                val = self.queue.get_nowait()
                break
            except queue.Empty:
                time.sleep(0.001)
                continue
        if val is None:
            raise StopIteration
        return val

    def __del__(self):
        if self.process is not None:
            self.process.terminate()
