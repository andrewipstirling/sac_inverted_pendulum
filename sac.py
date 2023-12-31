import random
import time
import numpy as np
import tensorflow as tf
import core
from utils import logx

class ReplayBuffer:

    def __init__(self, obs_dim, act_dim, size):
        self.obs1_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.obs2_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.acts_buf = np.zeros([size, act_dim], dtype=np.float32)
        self.rews_buf = np.zeros(size, dtype=np.float32)
        self.done_buf = np.zeros(size, dtype=np.float32)
        self.ptr, self.size, self.max_size = 0, 0, size

    def store(self, obs, act, rew, next_obs, done):
        self.obs1_buf[self.ptr, :] = obs
        self.obs2_buf[self.ptr, :] = next_obs
        self.acts_buf[self.ptr, :] = act
        self.rews_buf[self.ptr] = rew
        self.done_buf[self.ptr] = done
        self.ptr = (self.ptr + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

    def sample_batch(self, batch_size=32):
        idxs = np.random.randint(0, self.size, size=batch_size)
        return dict(obs1=tf.convert_to_tensor(self.obs1_buf[idxs]),
                    obs2=tf.convert_to_tensor(self.obs2_buf[idxs]),
                    acts=tf.convert_to_tensor(self.acts_buf[idxs]),
                    rews=tf.convert_to_tensor(self.rews_buf[idxs]),
                    done=tf.convert_to_tensor(self.done_buf[idxs]))

def sac(env_fn, actor_critic=core.mlp_actor_critic, ac_kwargs=None, seed=0,
        total_steps=1_000_000, log_every=10_000, replay_size=1_000_000,
        gamma=0.99, polyak=0.995, lr=0.001, alpha=0.2, batch_size=256,
        start_steps=10_000, update_after=1000, update_every=50,
        num_test_episodes=10, max_ep_len=1000, logger_kwargs=None,
        save_freq=int(1e4), save_path=None):
    
    """Soft Actor-Critic (SAC), documentation from 
    https://spinningup.openai.com/en/latest/algorithms/sac.html


    Params:
        env_fn : A function which creates a copy of the environment.
            The environment must satisfy the OpenAI Gym API.

        actor_critic: A function which takes in placeholder 
            symbols for state, x_ph, and action, a_ph, and returns 
            the main outputs from the agents Tensorflow computation graph:

        ac_kwargs (dict): Any kwargs appropriate for the actor_critic
            function you provided to SAC.

        seed (int): Seed for random number generators.

        total_steps (int): Number of environment interactions to run and train
            the agent.

        log_every (int): Number of environment interactions that should elapse
            between dumping logs.

        replay_size (int): Maximum length of replay buffer.

        gamma (float): Discount factor. (Always between 0 and 1.)

        polyak (float): Interpolation factor in polyak averaging for target
            networks. 

        lr (float): Learning rate (used for both policy and value learning).

        alpha (float): Entropy regularization coefficient. (Equivalent to
            inverse of reward scale in the original SAC paper.)

        batch_size (int): Minibatch size for SGD.

        start_steps (int): Number of steps for uniform-random action selection,
            before running real policy. Helps exploration.

        update_after (int): Number of env interactions to collect before
            starting to do gradient descent updates. Ensures replay buffer
            is full enough for useful updates.

        update_every (int): Number of env interactions that should elapse
            between gradient descent updates. Note: Regardless of how long
            you wait between updates, the ratio of env steps to gradient steps
            is locked to 1.

        num_test_episodes (int): Number of episodes to test the deterministic
            policy at the end of each epoch.

        max_ep_len (int): Maximum length of trajectory / episode / rollout.

        logger_kwargs (dict): Keyword args for EpochLogger.

        save_freq (int): How often (in terms of environment iterations) to save
            the current policy.

        save_path (str): The path specifying where to save the trained model.
    """
    config = locals()
    # logger code copied from https://github.com/openai/spinningup/blob/master/spinup/utils/logx.py
    logger = logx.EpochLogger(**(logger_kwargs or {}))
    logger.save_config(config)

    random.seed(seed)
    tf.random.set_seed(seed)
    np.random.seed(seed)

    env, test_env = env_fn(), env_fn()
    obs_dim = env.observation_space.shape[0]
    act_dim = env.action_space.shape[0]
    # Set dimensions of observation and action spaces

    # Give policy info on action and observation spaces
    ac_kwargs = ac_kwargs or {}
    ac_kwargs['action_space'] = env.action_space
    ac_kwargs['observation_space'] = env.observation_space

    # The experience buffer
    replay_buffer = ReplayBuffer(obs_dim=obs_dim, act_dim=act_dim,
                                 size=replay_size)

    # Get actor critic
    actor, critic = actor_critic(**ac_kwargs)

    # Two critics to avoid overestimation error
    critic1 = critic
    critic2 = tf.keras.models.clone_model(critic)

    input_shape = [(None, obs_dim), (None, act_dim)]


    # Set weights for each critic
    critic1.build(input_shape)
    target_critic1 = tf.keras.models.clone_model(critic)
    target_critic1.set_weights(critic1.get_weights())

    critic2.build(input_shape)
    target_critic2 = tf.keras.models.clone_model(critic)
    target_critic2.set_weights(critic2.get_weights())

    # Create variables used by optimizer
    critic_variables = critic1.trainable_variables + critic2.trainable_variables

    # Set up optimizer to later calculate gradients
    optimizer = tf.keras.optimizers.legacy.Adam(learning_rate=lr)

    # For use by tensorflow
    @tf.function
    def get_action(o, deterministic=tf.constant(False)):
        mu, pi, _ = actor(tf.expand_dims(o, 0))
        if deterministic:
            return mu[0]
        else:
            #sample policy if non-deterministic
            return pi[0]

    # Learning step for soft-actor critic
    # 12 - 15 from pseudo code
    @tf.function
    def learn_on_batch(obs1, obs2, acts, rews, done):
        with tf.GradientTape(persistent=True) as g:
            # Main outputs from computation graph.
            # Compute policy paramerters pi
            # Compute entropy, logp_pi
            _, pi, logp_pi = actor(obs1)
            q1 = critic1([obs1, acts])
            q2 = critic2([obs1, acts])

            # Compute Q-values for the current observations 
            # and actions sampled from the policy.
            q1_pi = critic1([obs1, pi])
            q2_pi = critic2([obs1, pi])

            # Get actions and log probs of actions for next states.
            _, pi_next, logp_pi_next = actor(obs2)

            # Get target for the Q functions
            # Calculated using current policy, for each critic
            target_q1 = target_critic1([obs2, pi_next])
            target_q2 = target_critic2([obs2, pi_next])

            ## SAC losses calculations
            # Get the minimum Q-value btween the two critics for the current policy
            min_q_pi = tf.minimum(q1_pi, q2_pi)
            min_target_q = tf.minimum(target_q1, target_q2)

            # Entropy-regularized Bellman backup for Q functions.
            # Using Clipped Double-Q targets.
            q_backup = tf.stop_gradient(rews + gamma * (1 - done) * (
                min_target_q - alpha * logp_pi_next))

            # Soft actor-critic losses.
            pi_loss = tf.reduce_mean(alpha * logp_pi - min_q_pi)
            q1_loss = 0.5 * tf.reduce_mean((q_backup - q1) ** 2)
            q2_loss = 0.5 * tf.reduce_mean((q_backup - q2) ** 2)
            value_loss = q1_loss + q2_loss

        # Compute gradients and do updates.
        # Get actor gradients
        actor_gradients = g.gradient(pi_loss, actor.trainable_variables)
        # Perform updates to actor network
        optimizer.apply_gradients(
            zip(actor_gradients, actor.trainable_variables))
        # Get critic gradients
        critic_gradients = g.gradient(value_loss, critic_variables)
        # Update the critic network
        optimizer.apply_gradients(
            zip(critic_gradients, critic_variables))
        # delete to free space
        del g

        # Polyak averaging for target variables
        # Used to slowly update target networks, stabilizes training
        # Step 15 of pseudocode
        # phi_target,i = \rho*\phi_targ,i + (1-\rho)\phi_i
        for v, target_v in zip(critic1.trainable_variables,
                               target_critic1.trainable_variables):
            target_v.assign(polyak * target_v + (1 - polyak) * v)
        for v, target_v in zip(critic2.trainable_variables,
                               target_critic2.trainable_variables):
            target_v.assign(polyak * target_v + (1 - polyak) * v)

        # Return Dictionary
        return dict(pi_loss=pi_loss,
                    q1_loss=q1_loss,
                    q2_loss=q2_loss,
                    q1=q1,
                    q2=q2,
                    logp_pi=logp_pi)

    def test_agent():
        for _ in range(num_test_episodes):
            o, d, ep_ret, ep_len = test_env.reset(), False, 0, 0
            o = o[0]
            # test until not done, or max episode length reached
            while not (d or (ep_len == max_ep_len)):
                # Take deterministic actions at test time
                # Perform step in the test environment
                o, r, d, d2, _ = test_env.step(
                    get_action(tf.convert_to_tensor(o), tf.constant(True)))
                ep_ret += r
                ep_len += 1
                d = d or d2
            # Add to the logger
            logger.store(TestEpRet=ep_ret, TestEpLen=ep_len)

    start_time = time.time()
    o, ep_ret, ep_len = env.reset(), 0, 0
    o = o[0]

    # Main loop: collect experience in env and update/log each epoch.
    for t in range(total_steps):
        iter_time = time.time()

        # Until start_steps have elapsed, randomly sample actions
        # from a uniform distribution for better exploration. Afterwards,
        # use the learned policy.

        # At the beginning t<start_steps, randomly sample actions
        # This provides better exploration
        # if t>start_steps, use the learned policy
        if t > start_steps:
            a = get_action(tf.convert_to_tensor(o))
        else:
            a = env.action_space.sample()

        # Take action and step the environment
        o2, r, d, d2, _ = env.step(a)
        ep_ret += r
        ep_len += 1

        # Ignore the "done" signal if it comes from hitting max time
        d = False if ep_len == max_ep_len else d
        d = d or d2

        # Add experience to the replay buffer
        replay_buffer.store(o, a, r, o2, d)

        # Update most recent observation
        o = o2

        # If end of trajectory
        # Reset Environment state
        # Step 8 of pseudocode
        if d or (ep_len == max_ep_len):
            logger.store(EpRet=ep_ret, EpLen=ep_len)
            o, ep_ret, ep_len = env.reset(), 0, 0
            o = o[0]

        # Update 
        if t >= update_after and t % update_every == 0:
            for _ in range(update_every):
                # Randomly sample a batch of transitions
                batch = replay_buffer.sample_batch(batch_size)
                # Update parameters
                # Lines 12 - 15 of pseudocode
                results = learn_on_batch(**batch)
                logger.store(LossPi=results['pi_loss'],
                             LossQ1=results['q1_loss'],
                             LossQ2=results['q2_loss'],
                             Q1Vals=results['q1'],
                             Q2Vals=results['q2'],
                             LogPi=results['logp_pi'])

        logger.store(StepsPerSecond=(1 / (time.time() - iter_time)))

        # End of epoch wrap-up.
        if ((t + 1) % log_every == 0) or (t + 1 == total_steps):
            # Test the performance of the deterministic version of the agent.
            test_agent()

            # Log info about epoch.
            logger.log_tabular('EpRet', with_min_and_max=True)
            logger.log_tabular('TestEpRet', with_min_and_max=True)
            logger.log_tabular('EpLen', average_only=True)
            logger.log_tabular('TestEpLen', average_only=True)
            logger.log_tabular('TotalEnvInteracts', t + 1)
            logger.log_tabular('Q1Vals', with_min_and_max=True)
            logger.log_tabular('Q2Vals', with_min_and_max=True)
            logger.log_tabular('LogPi', with_min_and_max=True)
            logger.log_tabular('LossPi', average_only=True)
            logger.log_tabular('LossQ1', average_only=True)
            logger.log_tabular('LossQ2', average_only=True)

            logger.log_tabular('StepsPerSecond', average_only=True)
            logger.log_tabular('Time', time.time() - start_time)

            logger.dump_tabular()

        # Save model.
        if ((t + 1) % save_freq == 0) or (t + 1 == total_steps):
            if save_path is not None:
                tf.keras.models.save_model(actor, save_path)