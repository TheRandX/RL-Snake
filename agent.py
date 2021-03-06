import json
import numpy as np
import torch
from time import sleep
import policy
from torch.utils.tensorboard import SummaryWriter
import algorithms

__DEFAULT_TENSOR__ = torch.double
algorithms.__DEFAULT_TENSOR__ = __DEFAULT_TENSOR__

class Agent():

    def __init__(self, tag, env, use_gpu=False, log=False):
        self.tag = tag
        self.env = env

        self.device = "cuda" if torch.cuda.is_available() and use_gpu else "cpu"
        print("Using " + self.device + "\n")

        if log:
            self.writer = SummaryWriter()

    # Creates new model for training
    @classmethod
    def for_training(cls, tag, hyperparam_path, env, log=False, use_gpu=False):
        agent = cls(tag, env, use_gpu, log)

        f = open(hyperparam_path)
        hyperparams = json.load(f)

        agent.learning_rate = hyperparams['learning_rate']
        agent.max_epochs = hyperparams['train_epochs']
        agent.lr_decay_rate = hyperparams['lr_decay_rate']

        hp_dict = {"learning_rate": agent.learning_rate,
                   "max_epochs": agent.max_epochs,
                   "lr_decay_rate": agent.lr_decay_rate
                   }

        connection_mode = hyperparams['connection_mode']
        if hyperparams['architecture'] == 'FNN':
            layer_connections = hyperparams['hidden_layers']
            layer_connections = np.power(int(np.prod(env.observation_space.shape)), layer_connections) if connection_mode == "exponentiative" else layer_connections
            layer_connections = np.insert(layer_connections, 0, int(np.prod(env.observation_space.shape)))
            layer_connections = np.append(layer_connections, int(env.action_space.n)).astype(int).tolist()
            agent.policy = policy.FNNPolicy(layer_connections, output_distribution=True).to(dtype=__DEFAULT_TENSOR__,
                                                                                     device=agent.device)
        elif hyperparams['architecture'] == 'CNN':
            cnn_dict = hyperparams['CNN_hidden_layers']
            fnn_layers = hyperparams['FNN_hidden_layers']
            # Assumes square observation space
            input_size = cnn_dict['channels'][-1]*policy.__convolution_output_size__(env.observation_space.shape[-1][-1], cnn_dict['kernel_sizes'], cnn_dict['strides'])**2
            if connection_mode == "exponentiative":
                fnn_layers = np.power(input_size, fnn_layers)
            fnn_layers = np.insert(fnn_layers, 0, input_size)
            fnn_layers = np.append(fnn_layers, int(env.action_space.n)).astype(int).tolist()
            agent.policy = policy.CNNPolicy(cnn_dict, fnn_layers, output_distribution=True).to(dtype=__DEFAULT_TENSOR__, device=agent.device)

        elif hyperparams['architecture'] == 'actor_critic':

            agent.critic_coeff = hyperparams['critic_coeff']
            hp_dict['critic_coeff'] = agent.critic_coeff

            agent.entropy_coeff = hyperparams['entropy_coeff']
            hp_dict['entropy_coeff'] = agent.entropy_coeff

            agent.gamma = hyperparams['gamma']
            hp_dict['gamma'] = agent.gamma

            agent.regularize_returns = hyperparams['regularize_returns']
            hp_dict['regularize_returns'] = agent.regularize_returns

            critic_dict = hyperparams['critic']
            actor_dict = hyperparams['actor']

            actor_fnn_layers = actor_dict['FNN_layers']
            critic_fnn_layers = critic_dict['FNN_layers']

            actor_fnn_input_size = policy.__convolution_output_size__(env.observation_space.shape[-1][-1],
                                                                       actor_dict['CNN_layers']['kernel_sizes'],
                                                                       actor_dict['CNN_layers']['strides'])
            critic_fnn_input_size = policy.__convolution_output_size__(env.observation_space.shape[-1][-1],
                                                                       critic_dict['CNN_layers']['kernel_sizes'],
                                                                       critic_dict['CNN_layers']['strides'])
            # Assumes square environment
            actor_fnn_input_size *= actor_fnn_input_size * actor_dict['CNN_layers']['channels'][-1]
            critic_fnn_input_size *= critic_fnn_input_size * critic_dict['CNN_layers']['channels'][-1]

            if connection_mode == 'exponentiative':
                actor_fnn_layers = np.power(actor_fnn_input_size, actor_fnn_layers)
                critic_fnn_layers = np.power(critic_fnn_input_size, critic_fnn_layers)
            actor_fnn_layers = np.insert(actor_fnn_layers, 0, actor_fnn_input_size)
            critic_fnn_layers = np.insert(critic_fnn_layers, 0, critic_fnn_input_size)

            actor_fnn_layers = np.append(actor_fnn_layers, int(env.action_space.n)).astype(int).tolist()
            critic_fnn_layers = np.append(critic_fnn_layers, 1).astype(int).tolist()

            actor_dict['FNN_layers'] = actor_fnn_layers
            critic_dict['FNN_layers'] = critic_fnn_layers

            agent.policy = policy.Actor_Critic(critic_dict, actor_dict).to(dtype=__DEFAULT_TENSOR__, device=agent.device)

        if agent.writer is not None:
            agent.writer.add_hparams(hp_dict, {})

        try:
            optim_type = hyperparams['optimiser']
        except KeyError:
            optim_type = 'Adam'
        if optim_type == "SGD":
            agent.optimiser = torch.optim.SGD(agent.policy.parameters(), lr=agent.learning_rate)
        else:
            agent.optimiser = torch.optim.Adam(agent.policy.parameters(), lr=agent.learning_rate)
        return agent

    # Loads model from checkpoint
    @classmethod
    def for_inference(cls, tag, env, model_path, use_gpu=False):
        agent = cls(tag, env, use_gpu)
        if not agent.device == "cuda":
            checkpoint = torch.load(model_path, map_location="cpu")
            agent.policy = checkpoint['model']
            agent.policy.load_state_dict(checkpoint['state_dict'])
        else:
            agent.policy = torch.load(model_path)
        print(type(agent.policy))
        return agent


    def train_reinforce(self, epochs=100, episodes=30, use_baseline=False, use_causality=False):
        algorithms.REINFORCE(self.tag, self.env, self.policy, self.optimiser, self.device, self.writer, epochs, episodes, use_baseline, use_causality)

    def train_a2c(self, test_spacing=-1):
        test_func = self.test if test_spacing > 0 else None
        algorithms.A2C(self.tag, self.env, self.policy, self.optimiser, gamma= self.gamma, entropy_coeff= self.entropy_coeff, critic_coeff=self.critic_coeff,device= self.device,
                       regularize_returns= self.regularize_returns, recurrent_model= True, logger= self.writer, epochs= self.max_epochs, test_func=test_func,
                       test_spacing=test_spacing, lr_decay_rate=self.lr_decay_rate)

    def __create_greedy_policy__(self, behaviour_func):
        def policy(observation, h_a, h_v):
            (action_distribution, h_a_), (_, h_v_) = behaviour_func(observation, h_a, h_v)
            action = torch.argmax(action_distribution).item()
            return action, h_a_, h_v_
        return policy

    def __create_stochastic_policy__(self, behaviour_func):
        def policy(observation):
            action_distribution = behaviour_func(observation)
            action = torch.distributions.Categorical(probs= action_distribution).sample().item()
            return action
        return policy

    def test(self, episodes=10):
        self.policy.eval()
        gr_policy = self.__create_greedy_policy__(self.policy)

        for episode in range(episodes):
            done = False
            state = self.env.reset()
            h_a, h_v = None, None
            while not done:
                state = torch.tensor(state, dtype=torch.float, device=self.device)
                (action, h_a, h_v) = gr_policy(state, h_a, h_v)
                state, reward, done, info = self.env.step(action)
                self.env.render()
                sleep(0.2)
        self.policy.train()



