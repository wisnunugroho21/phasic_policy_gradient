import gym
from gym.envs.registration import register
    
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow.keras.layers import Dense
from tensorflow.keras import Model

import matplotlib.pyplot as plt
import numpy as np
import sys
import numpy
import time
import datetime

class Policy_Model(Model):
    def __init__(self, state_dim, action_dim):
        super(Policy_Model, self).__init__()

        self.d1     = Dense(256, activation='relu')
        self.d2     = Dense(128, activation='relu')

        self.actor  = Dense(action_dim, activation='tanh')
        self.critic = Dense(action_dim, activation='linear')
        
    def call(self, states):
        x = self.d1(states)
        x = self.d2(x)
        return self.actor(x), self.critic(x)

class Value_Model(Model):
    def __init__(self, state_dim, action_dim):
        super(Value_Model, self).__init__()   

        self.d1     = Dense(128, activation='relu')
        self.d2     = Dense(64, activation='relu')

        self.critic = Dense(action_dim, activation='linear')
        
    def call(self, states):
        x = self.d1(states)
        x = self.d2(x)
        return self.critic(x)

class PolicyMemory():
    def __init__(self):
        self.actions        = [] 
        self.states         = []
        self.rewards        = []
        self.dones          = []     
        self.next_states    = []

    def __len__(self):
        return len(self.dones)

    def get_all_tensor(self):
        states      = tf.constant(self.states, dtype = tf.float32)
        actions     = tf.constant(self.actions, dtype = tf.float32)
        rewards     = tf.expand_dims(tf.constant(self.rewards, dtype = tf.float32), 1)
        dones       = tf.expand_dims(tf.constant(self.dones, dtype = tf.float32), 1)
        next_states = tf.constant(self.next_states, dtype = tf.float32)
        
        return tf.data.Dataset.from_tensor_slices((states, actions, rewards, dones, next_states))

    def get_all(self):
        return self.states, self.actions, self.rewards, self.dones, self.next_states     

    def save_eps(self, state, action, reward, done, next_state):
        self.rewards.append(reward)
        self.states.append(state)
        self.actions.append(action)
        self.dones.append(done)
        self.next_states.append(next_state)  

    def save_all(self, states, actions, rewards, dones, next_states):
        self.actions      += actions
        self.states       += states
        self.rewards      += rewards
        self.dones        += dones
        self.next_states  += next_states      

    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.rewards[:]
        del self.dones[:]
        del self.next_states[:]

class AuxMemory():
    def __init__(self):
        self.states = []

    def __len__(self):
        return len(self.states)

    def get_all_tensor(self):
        states = tf.constant(self.states, dtype = tf.float32)        
        return tf.data.Dataset.from_tensor_slices(states)

    def save_all(self, states):
        self.states = self.states + states

    def clear_memory(self):
        del self.states[:]

class Continous():
    def sample(self, mean, std):
        distribution = tfp.distributions.Normal(mean, std)
        return distribution.sample()
        
    def entropy(self, mean, std):
        distribution = tfp.distributions.Normal(mean, std)
        return distribution.entropy()
      
    def logprob(self, mean, std, value_data):
        distribution = tfp.distributions.Normal(mean, std)
        return distribution.log_prob(value_data)

    def kl_divergence(self, mean1, std1, mean2, std2):
        distribution1 = tfp.distributions.Normal(mean1, std1)
        distribution2 = tfp.distributions.Normal(mean2, std2)

        return tfp.distributions.kl_divergence(distribution1, distribution2)

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
            
        return tf.stack(returns)
      
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
            
        return tf.stack(adv)

class TrulyPPO():
    def __init__(self, policy_kl_range, policy_params, value_clip, vf_loss_coef, entropy_coef, gamma, lam):
        self.policy_kl_range    = policy_kl_range
        self.policy_params      = policy_params
        self.value_clip         = value_clip
        self.vf_loss_coef       = vf_loss_coef
        self.entropy_coef       = entropy_coef

        self.distributions      = Continous()
        self.policy_function    = PolicyFunction(gamma, lam)

    # Loss for PPO  
    def compute_loss(self, action_mean, action_std, old_action_mean, old_action_std, values, old_values, next_values, actions, rewards, dones):
        # Don't use old value in backpropagation
        Old_values         = tf.stop_gradient(old_values)
        Old_action_mean    = tf.stop_gradient(old_action_mean)

        # Getting general advantages estimator
        Advantages      = self.policy_function.generalized_advantage_estimation(values, rewards, next_values, dones)
        Returns         = tf.stop_gradient(Advantages + values)
        Advantages      = tf.stop_gradient((Advantages - tf.math.reduce_mean(Advantages)) / (tf.math.reduce_std(Advantages) + 1e-6))

        # Finding the ratio (pi_theta / pi_theta__old):        
        logprobs        = self.distributions.logprob(action_mean, action_std, actions)
        Old_logprobs    = tf.stop_gradient(self.distributions.logprob(Old_action_mean, old_action_std, actions))
        ratios          = tf.math.exp(logprobs - Old_logprobs) # ratios = old_logprobs / logprobs

        # Finding KL Divergence                
        Kl              = self.distributions.kl_divergence(Old_action_mean, old_action_std, action_mean, action_std)

        # Combining TR-PPO with Rollback (Truly PPO)
        pg_loss         = tf.where(
                tf.logical_and(Kl >= self.policy_kl_range, ratios > 1),
                ratios * Advantages - self.policy_params * Kl,
                ratios * Advantages
        )
        pg_loss         = tf.math.reduce_mean(pg_loss)

        # Getting entropy from the action probability
        dist_entropy    = tf.math.reduce_mean(self.distributions.entropy(action_mean, action_std))

        # Getting critic loss by using Clipped critic value
        vpredclipped    = old_values + tf.clip_by_value(values - Old_values, -self.value_clip, self.value_clip) # Minimize the difference between old value and new value
        vf_losses1      = tf.math.square(Returns - values) * 0.5 # Mean Squared Error
        vf_losses2      = tf.math.square(Returns - vpredclipped) * 0.5 # Mean Squared Error
        critic_loss     = tf.math.reduce_mean(tf.math.maximum(vf_losses1, vf_losses2))           

        # We need to maximaze Policy Loss to make agent always find Better Rewards
        # and minimize Critic Loss 
        loss            = (critic_loss * self.vf_loss_coef) - (dist_entropy * self.entropy_coef) - pg_loss
        return loss

class JointAux():
    def __init__(self):
        self.distributions  = Continous()

    def compute_loss(self, action_mean, action_std, old_action_mean, old_action_std, values, Returns):
        # Stop gradient at old action
        Old_action_mean    = tf.stop_gradient(old_action_mean)

        # Finding KL Divergence and Aux Loss           
        Kl              = tf.math.reduce_mean(self.distributions.kl_divergence(Old_action_mean, old_action_std, action_mean, action_std))
        aux_loss        = tf.math.reduce_mean(tf.math.square(Returns - values) * 0.5)

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
        self.std                = tf.ones([1, action_dim])

        self.policy             = Policy_Model(state_dim, action_dim)
        self.policy_old         = Policy_Model(state_dim, action_dim)

        self.value              = Value_Model(state_dim, action_dim)
        self.value_old          = Value_Model(state_dim, action_dim)

        self.policy_memory      = PolicyMemory()
        self.policy_loss        = TrulyPPO(policy_kl_range, policy_params, value_clip, vf_loss_coef, entropy_coef, gamma, lam)

        self.aux_memory         = AuxMemory()
        self.aux_loss           = JointAux()
         
        self.optimizer          = tf.keras.optimizers.Adam(learning_rate = learning_rate)
        self.distributions      = Continous()        

    def save_eps(self, state, action, reward, done, next_state):
        self.policy_memory.save_eps(state, action, reward, done, next_state)

    def save_all(self, states, actions, rewards, dones, next_states):
        self.policy_memory.save_all(states, actions, rewards, dones, next_states)

    def act(self, state):
        state           = tf.expand_dims(tf.cast(state, dtype = tf.float32), 0)
        action_mean, _  = self.policy(state)

        # We don't need sample the action in Test Mode
        # only sampling the action in Training Mode in order to exploring the actions
        if self.is_training_mode:
            # Sample the action
            action  = self.distributions.sample(action_mean, self.std) 
        else:
            action  = tf.math.argmax(action_mean, 1)
              
        return tf.squeeze(action)

    # Get loss and Do backpropagation
    @tf.function
    def training_ppo(self, states, actions, rewards, dones, next_states):
        with tf.GradientTape() as tape:
            action_mean, _      = self.policy(states)
            values              = self.value(states)
            old_action_mean, _  = self.policy_old(states)
            old_values          = self.value_old(states)
            next_values         = self.value(next_states)

            loss                = self.policy_loss.compute_loss(action_mean, self.std, old_action_mean, self.std, values, old_values, next_values, actions, rewards, dones)

        gradients = tape.gradient(loss, self.policy.trainable_variables + self.value.trainable_variables)        
        self.optimizer.apply_gradients(zip(gradients, self.policy.trainable_variables + self.value.trainable_variables))

    @tf.function
    def training_aux(self, states):
        Returns = tf.stop_gradient(self.value(states))

        with tf.GradientTape() as tape:
            action_mean, values = self.policy(states)
            old_action_mean, _  = self.policy_old(states)

            joint_loss          = self.aux_loss.compute_loss(action_mean, self.std, old_action_mean, self.std, values, Returns)

        gradients = tape.gradient(joint_loss, self.policy.trainable_variables)        
        self.optimizer.apply_gradients(zip(gradients, self.policy.trainable_variables))

    # Update the model
    def update_ppo(self):
        for _ in range(self.PPO_epochs):       
            for states, actions, rewards, dones, next_states in self.policy_memory.get_all_tensor().batch(self.batchsize):
                self.training_ppo(states, actions, rewards, dones, next_states)

        # Clear the memory
        states, _, _, _, _ = self.policy_memory.get_all()
        self.aux_memory.save_all(states)
        self.policy_memory.clear_memory()

        # Copy new weights into old policy:
        self.policy_old.set_weights(self.policy.get_weights())
        self.value_old.set_weights(self.value.get_weights())

    def update_aux(self):
        # Optimize policy for K epochs:
        for _ in range(self.PPO_epochs): 
            for states in self.aux_memory.get_all_tensor().batch(self.batchsize):
                self.training_aux(states)

        # Clear the memory
        self.aux_memory.clear_memory()

        # Copy new weights into old policy:
        self.policy_old.set_weights(self.policy.get_weights())

    def save_weights(self):
        self.policy.save_weights('bipedalwalker_w/policy_ppo', save_format='tf')
        self.value.save_weights('bipedalwalker_w/critic_ppo', save_format='tf')
        
    def load_weights(self):
        self.policy.load_weights('bipedalwalker_w/policy_ppo')
        self.value.load_weights('bipedalwalker_w/value_ppo')

class VectorEnv():
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
        states          = self.envs.reset()    
        done            = False
        total_reward    = 0
        eps_time        = 0
        ############################################ 
        for _ in range(self.n_update * self.n_aux_update):
            actions     = self.agent.act(states).numpy()

            action_gym  = np.clip(actions, -1.0, 1.0) * self.max_action
            datas       = self.envs.step(action_gym)

            rewards     = []
            next_states = []
            for state, action, memory, data in zip(states, actions, self.memories, datas):
                next_state, reward, done, _ = data
                rewards.append(reward)
                next_states.append(next_state)
                
                if self.training_mode:
                    memory.save_eps(state.tolist(), action.tolist(), reward, float(done), next_state.tolist())
            
            eps_time += 1 
            self.t_updates += 1
            total_reward += np.mean(rewards)
                
            states = next_states            
                    
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
    load_weights        = False # If you want to load the agent, set this to True
    save_weights        = False # If you want to save the agent, set this to True
    training_mode       = True # If you want to train the agent, set this to True. But set this otherwise if you only want to test it
    reward_threshold    = 300 # Set threshold for reward. The learning will stop if reward has pass threshold. Set none to sei this off
    using_google_drive  = False

    render              = False # If you want to display the image, set this to True. Turn this off if you run this in Google Collab
    n_update            = 1024 # How many episode before you update the Policy. Recommended set to 128 for Discrete
    n_plot_batch        = 100000000 # How many episode you want to plot the result
    n_episode           = 100000 # How many episode you want to run
    n_saved             = 10 # How many episode to run before saving the weights

    policy_kl_range     = 0.03 # Set to 0.0008 for Discrete
    policy_params       = 5 # Set to 20 for Discrete
    value_clip          = 5.0 # How many value will be clipped. Recommended set to the highest or lowest possible reward
    entropy_coef        = 0.0 # How much randomness of action you will get
    vf_loss_coef        = 1.0 # Just set to 1
    batchsize           = 32 # How many batch per update. size of batch = n_update / batchsize. Rocommended set to 4 for Discrete
    PPO_epochs          = 10 # How many epoch per update
    n_aux_update        = 5
    max_action          = 1.0
    
    gamma               = 0.99 # Just set to 0.99
    lam                 = 0.95 # Just set to 0.95
    learning_rate       = 3e-4 # Just set to 0.95
    #############################################
    writer              = tf.summary.create_file_writer('logs')

    env_name            = 'LunarLanderContinuous-v2' # Set the env you want
    env                 = [gym.make(env_name) for _ in range(2)]

    state_dim           = env[0].observation_space.shape[0]
    action_dim          = env[0].action_space.shape[0]

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

            print('Episode: {} \t t_reward: {} \t time: {} \t '.format(i_episode, total_reward, eps_time))
            with writer.as_default():
              tf.summary.scalar('rewards', total_reward, step = i_episode)

    except KeyboardInterrupt:        
        print('\nTraining has been Shutdown \n')

    finally:
        finish = time.time()
        timedelta = finish - start
        print('Timelength: {}'.format(str( datetime.timedelta(seconds = timedelta) )))

if __name__ == '__main__':
    main()