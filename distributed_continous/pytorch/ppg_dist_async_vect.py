import gym
from gym.envs.registration import register
    
import torch
import torch.nn as nn
from torch.distributions import Categorical
from torch.distributions.kl import kl_divergence
from torch.utils.data import Dataset, DataLoader
from torch.optim import Adam
from torch.utils.tensorboard import SummaryWriter

import matplotlib.pyplot as plt
import numpy as np
import sys
import numpy
import time
import datetime

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")  
dataType = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor

class Policy_Model(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Policy_Model, self).__init__()

        self.nn_layer = nn.Sequential(
                nn.Linear(state_dim, 1280),
                nn.ReLU(),
                nn.Linear(1280, 1280),
                nn.ReLU()
              ).float().to(device)

        self.actor_layer = nn.Sequential(
                nn.Linear(1280, action_dim),
                nn.Softmax(-1)
              ).float().to(device)

        self.critic_layer = nn.Sequential(
                nn.Linear(1280, 1)
              ).float().to(device)
        
    def forward(self, states):
        x = self.nn_layer(states)
        return self.actor_layer(x), self.critic_layer(x)

class Value_Model(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Value_Model, self).__init__()   

        self.nn_layer = nn.Sequential(
                nn.Linear(state_dim, 640),
                nn.ReLU(),
                nn.Linear(640, 640),
                nn.ReLU(),
                nn.Linear(640, 1)
              ).float().to(device)
        
    def forward(self, states):
        return self.nn_layer(states)

class PolicyMemory(Dataset):
    def __init__(self):
        self.actions        = [] 
        self.states         = []
        self.rewards        = []
        self.dones          = []     
        self.next_states    = []

    def __len__(self):
        return len(self.dones)

    def __getitem__(self, idx):
        return np.array(self.states[idx], dtype = np.float32), np.array(self.actions[idx], dtype = np.float32), \
            np.array([self.rewards[idx]], dtype = np.float32), np.array([self.dones[idx]], dtype = np.float32), np.array(self.next_states[idx], dtype = np.float32)      

    def get_all(self):
        return self.states, self.actions, self.rewards, self.dones, self.next_states        
    
    def save_all(self, states, actions, rewards, dones, next_states):
        self.actions = self.actions + actions
        self.states = self.states + states
        self.rewards = self.rewards + rewards
        self.dones = self.dones + dones
        self.next_states = self.next_states + next_states
    
    def save_eps(self, state, action, reward, done, next_state):
        self.rewards.append(reward)
        self.states.append(state)
        self.actions.append(action)
        self.dones.append(done)
        self.next_states.append(next_state)

    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.rewards[:]
        del self.dones[:]
        del self.next_states[:]  

class AuxMemory(Dataset):
    def __init__(self):
        self.states = []

    def __len__(self):
        return len(self.states)

    def __getitem__(self, idx):
        return np.array(self.states[idx], dtype = np.float32)

    def save_all(self, states):
        self.states = self.states + states

    def clear_memory(self):
        del self.states[:]

class Discrete():
    def sample(self, datas):
        distribution = Categorical(datas)
        return distribution.sample().float().to(device)
        
    def entropy(self, datas):
        distribution = Categorical(datas)    
        return distribution.entropy().float().to(device)
      
    def logprob(self, datas, value_data):
        distribution = Categorical(datas)
        return distribution.log_prob(value_data).unsqueeze(1).float().to(device)

    def kl_divergence(self, datas1, datas2):
        distribution1 = Categorical(datas1)
        distribution2 = Categorical(datas2)

        return kl_divergence(distribution1, distribution2).unsqueeze(1).float().to(device)  

class PolicyFunction():
    def __init__(self, gamma = 0.99, lam = 0.95):
        self.gamma  = gamma
        self.lam    = lam

    def monte_carlo_discounted(self, rewards, dones):
        running_add = 0
        returns     = []        
        
        for step in reversed(range(len(rewards))):
            running_add = rewards[step] + (1.0 - dones[step]) * self.gamma * running_add
            returns.insert(0, running_add)
            
        return torch.stack(returns)
      
    def temporal_difference(self, reward, next_value, done):
        q_values = reward + (1 - done) * self.gamma * next_value           
        return q_values
      
    def generalized_advantage_estimation(self, values, rewards, next_values, dones):
        gae     = 0
        adv     = []     

        delta   = rewards + (1.0 - dones) * self.gamma * next_values - values          
        for step in reversed(range(len(rewards))):
            gae = delta[step] + (1.0 - dones[step]) * self.gamma * self.lam * gae
            adv.insert(0, gae)
            
        return torch.stack(adv)

class TrulyPPO():
    def __init__(self, policy_kl_range, policy_params, value_clip, vf_loss_coef, entropy_coef, gamma, lam):
        self.policy_kl_range    = policy_kl_range
        self.policy_params      = policy_params
        self.value_clip         = value_clip
        self.vf_loss_coef       = vf_loss_coef
        self.entropy_coef       = entropy_coef

        self.distributions      = Discrete()
        self.policy_function    = PolicyFunction(gamma, lam)

    # Loss for PPO  
    def compute_loss(self, action_probs, old_action_probs, values, old_values, next_values, actions, rewards, dones):
        # Don't use old value in backpropagation
        Old_values          = old_values.detach()
        Old_action_probs    = old_action_probs.detach()     

        # Getting general advantages estimator and returns
        Advantages      = self.policy_function.generalized_advantage_estimation(values, rewards, next_values, dones)
        Returns         = (Advantages + values).detach()
        Advantages      = ((Advantages - Advantages.mean()) / (Advantages.std() + 1e-6)).detach()

        # Finding the ratio (pi_theta / pi_theta__old): 
        logprobs        = self.distributions.logprob(action_probs, actions)
        Old_logprobs    = self.distributions.logprob(Old_action_probs, actions).detach()

        # Finding Surrogate Loss
        ratios          = (logprobs - Old_logprobs).exp() # ratios = old_logprobs / logprobs        
        Kl              = self.distributions.kl_divergence(old_action_probs, action_probs)

        pg_targets  = torch.where(
            (Kl >= self.policy_kl_range) & (ratios > 1),
            ratios * Advantages - self.policy_params * Kl,
            ratios * Advantages
        )
        pg_loss     = pg_targets.mean()

        # Getting Entropy from the action probability 
        dist_entropy    = self.distributions.entropy(action_probs).mean()

        # Getting Critic loss by using Clipped critic value
        if self.value_clip is None:
            critic_loss   = ((Returns - values).pow(2) * 0.5).mean()
        else:
            vpredclipped  = old_values + torch.clamp(values - Old_values, -self.value_clip, self.value_clip) # Minimize the difference between old value and new value
            vf_losses1    = (Returns - values).pow(2) * 0.5 # Mean Squared Error
            vf_losses2    = (Returns - vpredclipped).pow(2) * 0.5 # Mean Squared Error        
            critic_loss   = torch.max(vf_losses1, vf_losses2).mean() 

        # We need to maximaze Policy Loss to make agent always find Better Rewards
        # and minimize Critic Loss 
        loss = (critic_loss * self.vf_loss_coef) - (dist_entropy * self.entropy_coef) - pg_loss
        return loss

class JointAux():
    def __init__(self):
        self.distributions  = Discrete()

    def compute_loss(self, action_probs, old_action_probs, values, Returns):
        # Don't use old value in backpropagation
        Old_action_probs    = old_action_probs.detach()

        # Finding KL Divergence                
        Kl              = self.distributions.kl_divergence(Old_action_probs, action_probs).mean()
        aux_loss        = ((Returns - values).pow(2) * 0.5).mean()

        return aux_loss + Kl

class Agent():  
    def __init__(self, state_dim, action_dim, is_training_mode, policy_kl_range, policy_params, value_clip, entropy_coef, vf_loss_coef,
                 batchsize, PPO_epochs, gamma, lam, learning_rate):        
        self.policy_kl_range    = policy_kl_range 
        self.policy_params      = policy_params
        self.value_clip         = value_clip    
        self.entropy_coef       = entropy_coef
        self.vf_loss_coef       = vf_loss_coef
        self.batchsize          = batchsize
        self.PPO_epochs         = PPO_epochs
        self.is_training_mode   = is_training_mode
        self.action_dim         = action_dim     

        self.policy             = Policy_Model(state_dim, action_dim)
        self.policy_old         = Policy_Model(state_dim, action_dim)
        self.policy_optimizer   = Adam(self.policy.parameters(), lr = learning_rate)

        self.value              = Value_Model(state_dim, action_dim)
        self.value_old          = Value_Model(state_dim, action_dim)
        self.value_optimizer    = Adam(self.value.parameters(), lr = learning_rate)

        self.policy_memory      = PolicyMemory()
        self.policy_loss        = TrulyPPO(policy_kl_range, policy_params, value_clip, vf_loss_coef, entropy_coef, gamma, lam)

        self.aux_memory         = AuxMemory()
        self.aux_loss           = JointAux()
         
        self.distributions      = Discrete()        

        if is_training_mode:
          self.policy.train()
          self.value.train()
        else:
          self.policy.eval()
          self.value.eval()

    def save_eps(self, state, action, reward, done, next_state):
        self.policy_memory.save_eps(state, action, reward, done, next_state)

    def save_all(self, states, actions, rewards, dones, next_states):
        self.policy_memory.save_all(states, actions, rewards, dones, next_states)

    def act(self, state):
        state           = torch.FloatTensor(state).to(device).detach()
        action_probs, _ = self.policy(state)

        # We don't need sample the action in Test Mode
        # only sampling the action in Training Mode in order to exploring the actions
        if self.is_training_mode:
            # Sample the action
            action  = self.distributions.sample(action_probs) 
        else:
            action  = torch.argmax(action_probs, 1)  
              
        return action.cpu().numpy()

    # Get loss and Do backpropagation
    def training_ppo(self, states, actions, rewards, dones, next_states):
        action_probs, _     = self.policy(states)
        values              = self.value(states)
        old_action_probs, _ = self.policy_old(states)
        old_values          = self.value_old(states)
        next_values         = self.value(next_states)

        loss                = self.policy_loss.compute_loss(action_probs, old_action_probs, values, old_values, next_values, actions, rewards, dones)

        self.policy_optimizer.zero_grad()
        self.value_optimizer.zero_grad()

        loss.backward()

        self.policy_optimizer.step()
        self.value_optimizer.step()

    def training_aux(self, states):
        Returns                         = self.value(states).detach()

        action_probs, values            = self.policy(states)
        old_action_probs, _             = self.policy_old(states)

        joint_loss                      = self.aux_loss.compute_loss(action_probs, old_action_probs, values, Returns)

        self.policy_optimizer.zero_grad()
        joint_loss.backward()
        self.policy_optimizer.step()

    # Update the model
    def update_ppo(self):
        dataloader  = DataLoader(self.policy_memory, self.batchsize, shuffle = False)

        # Optimize policy for K epochs:        
        for _ in range(self.PPO_epochs):
            for states, actions, rewards, dones, next_states in dataloader:
                self.training_ppo(states.float().to(device), actions.float().to(device), \
                    rewards.float().to(device), dones.float().to(device), next_states.float().to(device))

        # Clear the memory
        states, _, _, _, _ = self.policy_memory.get_all()
        self.aux_memory.save_all(states)
        self.policy_memory.clear_memory()

        # Copy new weights into old policy:
        self.policy_old.load_state_dict(self.policy.state_dict())
        self.value_old.load_state_dict(self.value.state_dict())

    def update_aux(self):
        dataloader  = DataLoader(self.aux_memory, self.batchsize, shuffle = False)

        # Optimize policy for K epochs:
        for _ in range(self.PPO_epochs): 
            for states in dataloader:
                self.training_aux(states.float().to(device))

        # Clear the memory
        self.aux_memory.clear_memory()

        # Copy new weights into old policy:
        self.policy_old.load_state_dict(self.policy.state_dict())

    def save_weights(self):
        torch.save({
            'model_state_dict': self.policy.state_dict(),
            'optimizer_state_dict': self.policy_optimizer.state_dict()
            }, 'Pong/policy.tar')
        
        torch.save({
            'model_state_dict': self.value.state_dict(),
            'optimizer_state_dict': self.value_optimizer.state_dict()
            }, 'Pong/value.tar')
        
    def load_weights(self):
        policy_checkpoint = torch.load('Pong/policy.tar')
        self.policy.load_state_dict(policy_checkpoint['model_state_dict'])
        self.policy_optimizer.load_state_dict(policy_checkpoint['optimizer_state_dict'])

        value_checkpoint = torch.load('Pong/value.tar')
        self.value.load_state_dict(value_checkpoint['model_state_dict'])
        self.value_optimizer.load_state_dict(value_checkpoint['optimizer_state_dict'])

def prepro(I):
    I           = I[35:195] # crop
    I           = I[::2,::2, 0] # downsample by factor of 2
    I[I == 144] = 0 # erase background (background type 1)
    I[I == 109] = 0 # erase background (background type 2)
    I[I != 0]   = 1 # everything else (paddles, ball) just set to 1
    X           = I.astype(np.float32).ravel() # Combine items in 1 array 
    return X

class VectorEnv:
    def __init__(self, envs):
        self.envs = envs

    # Call this only once at the beginning of training (optional):
    def seed(self, seeds):
        assert len(self.envs) == len(seeds)
        return tuple(env.seed(s) for env, s in zip(self.envs, seeds))

    # Call this only once at the beginning of training:
    def reset(self):
        return tuple(env.reset() for env in self.envs)

    # Call this on every timestep:
    def step(self, actions):
        assert len(self.envs) == len(actions)

        return_values = []
        for env, a in zip(self.envs, actions):
            observation, reward, done, info = env.step(a)
            if done:
                observation = env.reset()
            return_values.append((observation, reward, done, info))
            
        return tuple(return_values)

    def render(self):
        for env in self.envs:
            env.render()

    # Call this at the end of training:
    def close(self):
        for env in self.envs:
            env.close()

class Runner():
    def __init__(self, envs, agent, render, training_mode, n_update, n_aux_update, max_action):
        self.envs       = VectorEnv(envs)
        self.memories   = [PolicyMemory() for _ in range(len(envs))]

        self.agent          = agent
        self.render         = render
        self.training_mode  = training_mode
        self.n_update       = n_update
        self.n_aux_update   = n_aux_update
        self.max_action     = max_action

        self.t_updates      = 0
        self.t_aux_updates  = 0

    def run_episode(self):
        ############################################
        obs             = self.envs.reset()  
        obs             = [prepro(ob) for ob in obs]  
        states          = obs

        done            = False
        total_reward    = 0
        eps_time        = 0
        ############################################ 
        for _ in range(self.n_update * self.n_aux_update):
            actions     = self.agent.act(states)
            actions_gym = [int(action) + 1 if action != 0 else 0 for action in actions] 

            datas       = self.envs.step(actions_gym)

            rewards     = []
            next_states = []
            next_obs    = []
            for state, action, memory, data, ob in zip(states, actions, self.memories, datas, obs):
                next_ob, reward, done, _    = data
                next_ob                     = prepro(next_ob)
                next_state                  = next_ob - ob

                rewards.append(reward)
                next_states.append(next_state)
                next_obs.append(next_ob)
                
                if self.training_mode:
                    memory.save_eps(state.tolist(), action.tolist(), reward, float(done), next_state.tolist())
            
            eps_time += 1 
            self.t_updates += 1
            total_reward += np.mean(rewards)
                
            states = next_states
            obs = next_obs            
                    
            if self.render:
                self.envs.render()
            
            if self.training_mode and self.n_update is not None and self.t_updates == self.n_update:
                for memory in self.memories:
                    temp_states, temp_actions, temp_rewards, temp_dones, temp_next_states = memory.get_all()
                    self.agent.save_all(temp_states, temp_actions, temp_rewards, temp_dones, temp_next_states)
                    memory.clear_memory()

                self.agent.update_ppo()
                self.t_updates = 0
                self.t_aux_updates += 1

                if self.t_aux_updates == self.n_aux_update:
                    self.agent.update_aux()
                    self.t_aux_updates = 0
                    
        return total_reward, eps_time

def plot(datas):
    print('----------')

    plt.plot(datas)
    plt.plot()
    plt.xlabel('Episode')
    plt.ylabel('Datas')
    plt.show()

    print('Max :', np.max(datas))
    print('Min :', np.min(datas))
    print('Avg :', np.mean(datas))

def main():
    ############## Hyperparameters ##############
    load_weights        = True # If you want to load the agent, set this to True
    save_weights        = True # If you want to save the agent, set this to True
    training_mode       = True # If you want to train the agent, set this to True. But set this otherwise if you only want to test it
    reward_threshold    = 300 # Set threshold for reward. The learning will stop if reward has pass threshold. Set none to sei this off
    using_google_drive  = False

    render              = False # If you want to display the image, set this to True. Turn this off if you run this in Google Collab
    n_update            = 1024 # How many episode before you update the Policy. Recommended set to 128 for Discrete
    n_plot_batch        = 1 # How many episode you want to plot the result
    n_episode           = 100000 # How many episode you want to run
    n_saved             = 10 # How many episode to run before saving the weights

    policy_kl_range     = 0.0008 # Set to 0.0008 for Discrete
    policy_params       = 20 # Set to 20 for Discrete
    value_clip          = 2.0 # How many value will be clipped. Recommended set to the highest or lowest possible reward
    entropy_coef        = 0.05 # How much randomness of action you will get
    vf_loss_coef        = 1.0 # Just set to 1
    batchsize           = 32 # How many batch per update. size of batch = n_update / batchsize. Rocommended set to 4 for Discrete
    PPO_epochs          = 4 # How many epoch per update
    n_aux_update        = 5
    max_action          = 1.0
    
    gamma               = 0.99 # Just set to 0.99
    lam                 = 0.95 # Just set to 0.95
    learning_rate       = 3e-4 # Just set to 0.95
    ############################################# 
    writer              = SummaryWriter()

    env_name            = 'PongDeterministic-v4' # Set the env you want
    env                 = [gym.make(env_name) for _ in range(2)]

    state_dim           = 80 * 80 #env[0].observation_space.shape[0]
    action_dim          = 3 #env[0].action_space.shape[0]

    agent               = Agent(state_dim, action_dim, training_mode, policy_kl_range, policy_params, value_clip, entropy_coef, vf_loss_coef,
                            batchsize, PPO_epochs, gamma, lam, learning_rate)  

    runner              = Runner(env, agent, render, training_mode, n_update, n_aux_update, max_action)
    #############################################     
    if using_google_drive:
        from google.colab import drive
        drive.mount('/test')

    if load_weights:
        agent.load_weights()
        print('Weight Loaded')

    print('Run the training!!')
    start = time.time()

    try:
        for i_episode in range(1, n_episode + 1):
            total_reward, eps_time = runner.run_episode()

            print('Episode {} \t t_reward: {} \t time: {} \t '.format(i_episode, total_reward, eps_time))

            if i_episode % n_plot_batch == 0:
                writer.add_scalar('Rewards', total_reward, i_episode)

            if save_weights:
                if i_episode % n_saved == 0:
                    agent.save_weights() 
                    print('weights saved')

    except KeyboardInterrupt:        
        print('\nTraining has been Shutdown \n')

    finally:
        finish = time.time()
        timedelta = finish - start
        print('Timelength: {}'.format(str( datetime.timedelta(seconds = timedelta) )))

if __name__ == '__main__':
    main()