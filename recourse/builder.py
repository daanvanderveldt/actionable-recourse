import time
import warnings
import numpy as np
import pandas as pd
from itertools import chain
from collections import defaultdict
from recourse.defaults import DEFAULT_SOLVER, SUPPORTED_SOLVERS, _SOLVER_TYPE_CPX, _SOLVER_TYPE_CBC
from recourse.helper_functions import parse_classifier_args
from recourse.action_set import ActionSet

try:
    from cplex import Cplex, SparsePair
    from recourse.cplex_helper import set_cpx_parameters, set_cpx_display_options, set_cpx_time_limit, set_cpx_node_limit, toggle_cpx_preprocessing, DEFAULT_CPLEX_PARAMETERS
except ImportError:
    pass

try:
    from pyomo.core import Objective, Constraint, Var, Param, Set, AbstractModel, Binary, minimize
    from pyomo.opt import SolverFactory, SolverStatus, TerminationCondition
except ImportError:
    pass


class RecourseBuilder(object):

    _default_print_flag = True
    _default_check_flag = True
    _default_node_limit = float('inf')
    _default_time_limit = float('inf')
    _default_mip_cost_type = 'max'
    _default_enumeration_type = 'distinct_subsets'
    _valid_mip_cost_types = {'total', 'local', 'max'}
    _valid_enumeration_types = {'mutually_exclusive', 'distinct_subsets'}

    def __new__(cls, **kwargs):

        """Factory Method."""
        solver = kwargs.get("solver", DEFAULT_SOLVER)
        if not solver in SUPPORTED_SOLVERS:
            raise ValueError("pick solver in: %r" % SUPPORTED_SOLVERS)
        return super().__new__(BUILDER_TO_SOLVER[solver])


    def __init__(self, action_set, x = None, **kwargs):
        """
        :param x: vector of input variables for person x
        :param intercept: intercept value of score function
        :param coefs: coefficients of score function
        :param action_set: action set
        :param params: parameters for flipset form/flipset generation
                       (e.g. type of cost function to use / max items etc.)
        """

        # attach coefficients
        self._coefficients, self._intercept = parse_classifier_args(**kwargs)

        # attach action set
        assert isinstance(action_set, ActionSet)
        assert len(action_set) == len(self._coefficients)
        if not action_set.aligned:
            action_set.align(self._coefficients)

        self._action_set = action_set

        # add indices
        self._variable_names = action_set.name
        self._variable_index = {n: j for j, n in enumerate(self._variable_names)}
        self._actionable_indices = [j for j, v in enumerate(action_set.actionable) if v]

        # flags
        self.print_flag = kwargs.get('print_flag', self._default_print_flag)
        self.check_flag = kwargs.get('check_flag', self._default_check_flag)

        # initialize Cplex MIP
        self._mip = None
        self._mip_cost_type = kwargs.get('mip_cost_type', self._default_mip_cost_type)
        self._min_items = 0
        self._max_items = self.n_actionable

        # attach features
        self._x = None
        if x is not None:
            self.x = x

        assert self._check_rep()


    def _check_rep(self):
        """
        :return: True if representation invariants are true
        """
        if self.check_flag:
            assert self.n_variables == len(self._coefficients)
            assert self.n_variables == len(self._action_set)
            assert self._x is None or self.n_variables == len(self._x)
            assert isinstance(self._intercept, float)
            assert self.action_set.aligned
            assert 0 <= self._min_items <= self._max_items <= self.n_variables
        return True


    @property
    def check_flag(self):
        return self._check_flag


    @check_flag.setter
    def check_flag(self, flag):
        if flag is None:
            self._check_flag = bool(self._default_check_flag)
        elif isinstance(flag, bool):
            self._check_flag = bool(flag)
        else:
            raise AttributeError('check_flag must be boolean or None')


    #### printing ####
    @property
    def print_flag(self):
        return self._print_flag


    @print_flag.setter
    def print_flag(self, flag):
        if flag is None:
            self._print_flag = bool(self._default_print_flag)
        elif isinstance(flag, bool):
            self._print_flag = bool(flag)
        else:
            raise AttributeError('print_flag must be boolean or None')


    def __repr__(self):
        return str(self.action_set)


    #### immutable properties ####
    @property
    def action_set(self):
        return self._action_set


    @property
    def variable_names(self):
        return self._variable_names


    @property
    def n_variables(self):
        return len(self._coefficients)


    @property
    def variable_index(self):
        return self._variable_index


    @property
    def actionable_indices(self):
        return self._actionable_indices


    @property
    def n_actionable(self):
        return len(self._actionable_indices)


    #### feature values ####
    @property
    def x(self):
        return np.array(self._x)


    @x.setter
    def x(self, x):
        assert isinstance(x, (list, np.ndarray))
        x = np.array(x, dtype = np.float_).flatten()
        assert len(x) == self.n_variables
        self._x = x
        self.build_mip()

    #### model ####
    @property
    def coefficients(self):
        return self._coefficients

    @property
    def intercept(self):
        return float(self._intercept)


    def score(self, x = None):
        if x is None:
            x = self._x
        else:
            assert isinstance(x, (np.ndarray, list))
            x = np.array(x, dtype = np.float).flatten()
        return self._coefficients.dot(x) + self._intercept


    def prediction(self, x = None):
        return np.sign(self.score(x))

    #### MIP ####
    @property
    def mip(self):
        return self._mip


    @property
    def mip_cost_type(self):
        return self._mip_cost_type


    @property
    def mip_display(self):
        return self._mip_display_flag


    @property
    def mip_time_limit(self):
        return self._mip_time_limit


    @property
    def mip_node_limit(self):
        return self._mip_node_limit


    @mip_time_limit.setter
    def mip_time_limit(self, value):
        if value is None:
            value = self._default_time_limit
        elif isinstance(value, (float, int)) and value > 0.0:
            value = float(value)
        else:
            raise ValueError('time limit must be positive or inf')
        self._set_mip_time_limit(self.mip, value)
        self._mip_time_limit = value


    @mip_time_limit.setter
    def mip_node_limit(self, value):
        if value is None:
            value = self._default_node_limit
        elif isinstance(value, (float, int)) and value > 0:
            value = float(value)
        else:
            raise ValueError('node limit must be positive integer or inf')
        self._set_mip_node_limit(self.mip, value)
        self._mip_node_limit = value


    @mip_display.setter
    def mip_display(self, flag):
        """
        sets display flag for mip
        :param display_flag:
        :return:
        """
        if flag is None:
            flag = self._default_print_flag
        elif not isinstance(flag, bool):
            raise ValueError('mip_display must be boolean')
        self._set_mip_display(self.mip, flag)
        self._mip_node_limit = flag


    #### mip item limits ####
    def set_mip_item_limits(self, min_items = None, max_items = None):
        """
        changes limits for the number of items
        :param min_items:
        :param max_items:
        :return:
        """

        if min_items is None or max_items is None:
            return

        min_items = self.min_items if min_items is None else int(min_items)
        max_items = self.max_items if min_items is None else int(max_items)
        assert min_items <= max_items, 'incompatible sizes'

        min_items = int(min_items)
        if min_items != self.min_items:
            min_nnz_actions = float(self.n_actionable - min_items)
            self.set_mip_min_items(min_nnz_actions)
            self.min_items = min_items

        if max_items != self.max_items:
            max_nnz_actions = float(self.n_actionable - max_items)
            self.set_mip_max_items(min_nnz_actions)
            self.max_items = max_items

        return


    @property
    def min_items(self):
        return int(self._min_items)


    @min_items.setter
    def min_items(self, k):
        if k is None:
            self._min_items = 0
        else:
            k = int(k)
            assert k >= 0
            self._min_items = k


    @property
    def max_items(self):
        return int(self._max_items)


    @max_items.setter
    def max_items(self, k):
        if k is None:
            self._max_items = self.n_actionable
        else:
            k = int(k)
            assert k >= 0
            self._max_items = min(k, self.n_actionable)


    #### mip stamping ####
    def _get_mip_build_info(self, cost_function_type = 'percentile', validate = True):

        build_info = {}
        if self.mip_cost_type == 'local':
            cost_up = lambda c: np.log((1.0 - c[0])/(1.0 - c))
            cost_dn = lambda c: np.log((1.0 - c) / (1.0 - c[0]))
        else:
            cost_up = lambda c: c - c[0]
            cost_dn = lambda c: c[0] - c

        indices = defaultdict(list)
        if cost_function_type == 'percentile':

            actions, percentiles = self._action_set.feasible_grid(x = self._x, return_actions = True, return_percentiles = True, return_immutable = False)

            for n, a in actions.items():

                if len(a) >= 2:

                    c = percentiles[n]
                    if np.isclose(a[-1], 0.0):
                        a = np.flip(a, axis = 0)
                        c = np.flip(c, axis = 0)
                        c = cost_dn(c)
                    else:
                        c = cost_up(c)

                    # override numerical issues
                    bug_idx = np.logical_or(np.less_equal(c, 0.0), np.isclose(a, 0.0, atol = 1e-8))
                    bug_idx = np.flatnonzero(bug_idx).tolist()
                    bug_idx.pop(0)
                    if len(bug_idx) > 0:
                        c = np.delete(c, bug_idx)
                        a = np.delete(a, bug_idx)

                    idx = self._variable_index[n]
                    w = float(self._coefficients[idx])
                    #da = np.diff(a)
                    dc = np.diff(c)

                    info = {
                        'idx': idx,
                        'coef': w,
                        'actions': a.tolist(),
                        'costs': c.tolist(),
                        'action_var_name': ['a[%d]' % idx],
                        'action_ind_names': ['u[%d][%d]' % (idx, k) for k in range(len(a))],
                        'cost_var_name': ['c[%d]' % idx]
                        }

                    build_info[n] = info

                    indices['var_idx'].append(idx)
                    indices['coefficients'].append(w)
                    indices['action_off_names'].append(info['action_ind_names'][0])
                    indices['action_ind_names'].extend(info['action_ind_names'])
                    indices['action_var_names'].extend(info['action_var_name'])
                    indices['cost_var_names'].extend(info['cost_var_name'])
                    indices['action_lb'].append(float(np.min(a)))
                    indices['action_ub'].append(float(np.max(a)))
                    # indices['action_df'].append(float(np.min(da)))
                    indices['cost_ub'].append(float(np.max(c)))
                    indices['cost_df'].append(float(np.min(dc)))

        if validate:
            assert self._check_mip_build_info(build_info)

        return build_info, indices


    def _check_mip_build_info(self, build_info):

        for v in build_info.values():

            assert not np.isclose(v['coef'], 0.0)
            a = np.array(v['actions'])
            c = np.array(v['costs'])
            assert c[0] == 0.0
            assert a[0] == 0.0

            if np.sign(v['coef']) > 0:
                assert np.all(np.greater(a[1:], 0.0))
            else:
                assert np.all(np.less(a[1:], 0.0))

            assert len(a) >= 2
            assert len(a) == len(c)
            assert len(a) == len(np.unique(a))
            assert len(c) == len(np.unique(c))

        return True

    #### mip solutions ####
    @property
    def _empty_mip_solution_info(self):
        """
        :return: dictionary containing the output of the MIP.
                 default values correspond to a case where the model is infeasible
        """

        info = {
            'cost': float('inf'),
            'feasible': False,
            'status': 'no solution exists',
            #
            'costs': np.repeat(np.nan, self.n_variables),
            'actions': np.repeat(np.nan, self.n_variables),
            'upperbound': float('inf'),
            'lowerbound': float('inf'),
            'gap': float('inf'),
            #
            'iterations': 0,
            'nodes_processed': 0,
            'nodes_remaining': 0,
            'runtime': 0.0,
            }

        return info


    def _check_mip_solution(self, info):
        """
        :return: return True if making the change from the Flipset will actually 'flip' the prediction for the classifier
        """
        if info['feasible']:
            a = info['actions']
            all_idx = np.arange(len(a))
            static_idx = np.flatnonzero(np.isclose(a, 0.0, rtol = 1e-4))
            action_idx = np.setdiff1d(all_idx, static_idx)
            n_items = len(action_idx)
            assert n_items >= 1
            assert self.min_items <= n_items <= self.max_items

            try:
                assert np.all(np.isin(action_idx, self.actionable_indices))
            except AssertionError:
                warnings.warn('action set no in self.actionable_indices')

            x = self.x
            try:
                assert np.not_equal(self.prediction(x), self.prediction(x + a))
            except AssertionError:
                s = self.score(x + a)
                assert not np.isclose(self.score(x + a), 0.0, atol = 1e-4)
                warnings.warn('numerical issue: near-zero score(x + a) = %1.8f' % s)

            try:
                # check costs change -> action
                assert np.all(np.greater(info['costs'][action_idx], 0.0))
                assert np.all(np.isclose(info['costs'][static_idx], 0.0))

                # check total cost
                if self.mip_cost_type == 'max':
                    if not np.isclose(info['cost'], np.max(info['costs']), rtol = 1e-4):
                        warnings.warn('numerical issue: max_cost is %1.2f but maximum of cost[j] is %1.2f' % (info['cost'], np.max(info['costs'])))
                elif self.mip_cost_type == 'total':
                    assert np.isclose(info['cost'], np.sum(info['costs']))

            except AssertionError:
                warnings.warn('issue detected with %s' % str(info))

        return True


    def fit(self, time_limit = None, node_limit = None, display_flag = False):
        """
        Solve recourse problem
        :param time_limit:
        :param node_limit:
        :param display_flag:
        :return:
        """

        assert self._mip is not None, 'must first initialize recourse IP'

        # update time limit and node limit
        self.mip_display = display_flag
        self.mip_time_limit = time_limit
        self.mip_node_limit = node_limit

        # solve
        start_time = time.process_time()
        self.solve_mip()
        end_time = time.process_time() - start_time
        info = self.solution_info
        info['runtime'] = end_time
        return info


    def populate(self, total_items = 10, enumeration_type = 'distinct_subsets', time_limit = None, node_limit = None, display_flag = False):
        """
        Repeatedly solve recourse problem to recover successive optima
        :param total_items:
        :param enumeration_type:
        :param time_limit:
        :param node_limit:
        :param display_flag:
        :return:
        """
        assert self._mip is not None, 'must first initialize recourse IP'
        assert (isinstance(total_items, int) and total_items >= 1) or (isinstance(total_items, float) and total_items == float('inf'))
        assert enumeration_type in self._valid_enumeration_types

        # set the remove solution method
        if enumeration_type == 'mutually_exclusive':
            remove_solution = self.remove_all_features
        else:
            remove_solution = self.remove_feature_combination

        # update time limit and node limit
        self.mip_display = display_flag
        self.mip_time_limit = time_limit
        self.mip_node_limit = node_limit

        # enumerate soluitions
        k = 0
        all_info = []
        populate_start_time = time.process_time()
        while k < total_items:
            self.solve_mip()
            info = self.solution_info
            if not info['feasible']:
                if self.print_flag:
                    print('recovered all minimum-cost items')
                break
            all_info.append(info)
            remove_solution()
            k += 1

        if self.print_flag:
            print('obtained %d items in %1.1f seconds' % (k, time.process_time() - populate_start_time))

        return all_info




#### CPLEX

class _RecourseBuilderCPX(RecourseBuilder):


    _default_cplex_parameters = dict(DEFAULT_CPLEX_PARAMETERS)


    def __init__(self, action_set, x = None, **kwargs):
        """
        :param x: vector of input variables for person x
        :param intercept: intercept value of score function
        :param coefs: coefficients of score function
        :param action_set: action set
        :param params: parameters for flipset form/flipset generation
                       (e.g. type of cost function to use / max items etc.)
        """
        # initialize Cplex MIP
        self._cpx_parameters = kwargs.get('cplex_parameters', self._default_cplex_parameters)
        self._set_mip_time_limit = set_cpx_time_limit
        self._set_mip_node_limit = set_cpx_node_limit
        self._set_mip_display = lambda mip, flag: set_cpx_display_options(mip, display_lp = flag, display_mip = flag, display_parameters = flag)

        ## initialize base class
        super().__init__(action_set = action_set, x = x, **kwargs)


    #### building MIP ####
    def build_mip(self):
        """
        returns an optimization problem that can be solved to determine an item in a flipset for x
        :return:
        """

        # MIP parameters
        cost_type = self.mip_cost_type
        min_items = max(self.min_items, 1)
        max_items = self.max_items

        # cost/action information
        build_info, indices = self._get_mip_build_info()

        # initialize mip
        mip = Cplex()
        mip.set_problem_type(mip.problem_type.MILP)
        vars = mip.variables
        cons = mip.linear_constraints
        n_actionable = len(build_info)
        n_indicators = len(indices['action_ind_names'])

        # define a[j]
        vars.add(names = indices['action_var_names'],
                 types = ['C'] * n_actionable,
                 lb = indices['action_lb'],
                 ub = indices['action_ub'])

        # sum_j w[j] a[j] > -score
        cons.add(names = ['score'],
                 lin_expr = [SparsePair(ind = indices['action_var_names'], val = indices['coefficients'])],
                 senses = ['G'],
                 rhs = [-self.score()])

        # define indicators u[j][k] = 1 if a[j] = actions[j][k]
        vars.add(names = indices['action_ind_names'], types = ['B'] * n_indicators)

        # restrict a[j] to feasible values using a 1 of K constraint setup
        for info in build_info.values():

            # restrict a[j] to actions in feasible set and make sure exactly 1 indicator u[j][k] is on
            # 1. a[j]  =   sum_k u[j][k] * actions[j][k] - > 0.0   =   sum u[j][k] * actions[j][k] - a[j]
            # 2.sum_k u[j][k] = 1.0
            cons.add(names = ['set_a[%d]' % info['idx'], 'pick_a[%d]' % info['idx']],
                     lin_expr = [SparsePair(ind = info['action_var_name'] + info['action_ind_names'], val = [-1.0] + info['actions']),
                                 SparsePair(ind = info['action_ind_names'], val = [1.0] * len(info['actions']))],
                     senses = ["E", "E"],
                     rhs = [0.0, 1.0])

            # declare indicator variables as SOS set
            mip.SOS.add(type = "1", name = "sos_u[%d]" % info['idx'], SOS = SparsePair(ind = info['action_ind_names'], val = info['actions']))

        # limit number of features per action
        #
        # size := n_actionable - n_null where n_null := sum_j u[j][0] = sum_j 1[a[j] = 0]
        #
        # size <= max_size
        # n_actionable - sum_j u[j][0]  <=  max_size
        # n_actionable - max_size       <=  sum_j u[j][0]
        #
        # min_size <= size:
        # min_size          <=  n_actionable - sum_j u[j][0]
        # sum_j u[j][0]     <=  n_actionable - min_size
        size_expr = SparsePair(ind = indices['action_off_names'], val = [1.0] * n_actionable)
        cons.add(names = ['max_items', 'min_items'],
                 lin_expr = [size_expr, size_expr],
                 senses = ['G', 'L'],
                 rhs = [float(n_actionable - max_items), float(n_actionable - min_items)])

        # add constraints for cost function
        if cost_type in ('total', 'local'):
            indices.pop('cost_var_names')
            objval_pairs = list(chain(*[list(zip(v['action_ind_names'], v['costs'])) for v in build_info.values()]))
            mip.objective.set_linear(objval_pairs)

        elif cost_type == 'max':

            indices['max_cost_var_name'] = ['max_cost']
            indices['epsilon'] = np.min(indices['cost_df']) / np.sum(indices['cost_ub'])
            vars.add(names = indices['max_cost_var_name'] + indices['cost_var_names'],
                     types = ['C'] * (n_actionable + 1),
                     obj = [1.0] + [indices['epsilon']] * n_actionable)
            #lb = [0.0] * (n_actionable + 1)) # default values are 0.0

            cost_constraints = {
                'names': [],
                'lin_expr': [],
                'senses': ["E", "G"] * n_actionable,
                'rhs': [0.0, 0.0] * n_actionable,
                }

            for info in build_info.values():

                cost_constraints['names'].extend([
                    'def_cost[%d]' % info['idx'],
                    'set_max_cost[%d]' % info['idx']
                    ])

                cost_constraints['lin_expr'].extend([
                    SparsePair(ind = info['cost_var_name'] + info['action_ind_names'], val = [-1.0] + info['costs']),
                    SparsePair(ind = indices['max_cost_var_name'] + info['cost_var_name'], val = [1.0, -1.0])
                    ])

            cons.add(**cost_constraints)

            # old code (commented out for speed)
            #
            # vars.add(names = indices['cost_var_names'],
            #          types = ['C'] * n_actionable,
            #          obj = [indices['epsilon']] * n_actionable,
            #          #ub = [CPX_INFINITY] * n_actionable, #indices['cost_ub'], #indices['cost_ub'],
            #          lb = [0.0] * n_actionable)
            #
            # vars.add(names = indices['max_cost_var_name'],
            #          types = ['C'],
            #          obj = [1.0],
            #          #ub = [np.max(indices['cost_ub'])],
            #          lb = [0.0])
            #
            # for info in build_info.values():
            #     cost[j] = sum c[j][k] u[j][k]
            #     cons.add(names = ['def_cost[%d]' % info['idx']],
            #              lin_expr = [SparsePair(ind = info['cost_var_name'] + info['action_ind_names'], val = [-1.0] + info['costs'])]
            #              senses = ["E"],
            #              rhs = [0.0])
            #
            #     max_cost > cost[j]
            #     cons.add(names = ['set_max_cost[%d]' % info['idx']],
            #              lin_expr = [SparsePair(ind = indices['max_cost_var_name'] + info['cost_var_name'], val = [1.0, -1.0])],
            #              senses = ["G"],
            #              rhs = [0.0])


        mip = set_cpx_parameters(mip, self._cpx_parameters)
        self._mip = mip
        self._mip_indices = indices

    @property
    def mip_indices(self):
        return self._mip_indices


    #### MIP settings ###
    def set_mip_parameters(self, param = None):
        """
        updates MIP parameters
        :param param:
        :return:
        """
        if param is None:
            param = self._cpx_parameters

        self._mip = set_cpx_parameters(self._mip, param)


    ##### solving MIP ####
    def solve_mip(self):
        self.mip.solve()


    @property
    def solution_info(self):

        assert hasattr(self._mip, 'solution')
        mip = self._mip
        sol = mip.solution
        info = self._empty_mip_solution_info
        if sol.is_primal_feasible():

            indices = self._mip_indices
            variable_idx = indices['var_idx']

            # parse actions
            action_values = sol.get_values(indices['action_var_names'])

            if 'cost_var_names' in indices and self.mip_cost_type != 'total':
                cost_values = sol.get_values(indices['cost_var_names'])
            else:
                ind_idx = np.flatnonzero(np.array(sol.get_values(indices['action_ind_names'])))
                ind_names = [indices['action_ind_names'][int(k)] for k in ind_idx]
                cost_values = mip.objective.get_linear(ind_names)

            actions = np.zeros(self.n_variables)
            np.put(actions, variable_idx, action_values)

            costs = np.zeros(self.n_variables)
            np.put(costs, variable_idx, cost_values)

            info.update({
                'feasible': True,
                'status': sol.get_status_string(),
                #
                'actions': actions,
                'costs': costs,
                #
                'upperbound': sol.get_objective_value(),
                'lowerbound': sol.MIP.get_best_objective(),
                'gap': sol.MIP.get_mip_relative_gap(),
                #
                'iterations': sol.progress.get_num_iterations(),
                'nodes_processed': sol.progress.get_num_nodes_processed(),
                'nodes_remaining': sol.progress.get_num_nodes_remaining()
                })

            if self.mip_cost_type == 'max':
                info['cost'] = sol.get_values(indices['max_cost_var_name'])[0]
            else:
                info['cost'] = info['upperbound']

        else:

            info.update({
                'iterations': sol.progress.get_num_iterations(),
                'nodes_processed': sol.progress.get_num_nodes_processed(),
                'nodes_remaining': sol.progress.get_num_nodes_remaining()
                })

        return info


    #### Flipset methods (solver specific)
    def set_mip_min_items(self, n_items):
        """
        sets maximum number of non-zero elements in MIP (used by set_item_limits)
        :param n_items:
        :return:
        """
        self._mip.linear_constraints.set_rhs("min_items", n_items)


    def set_mip_max_items(self, n_items):
        """
        sets maximum number of non-zero elements in MIP (used by set_item_limits)
        :param n_items:
        :return:
        """
        self._mip.linear_constraints.set_rhs("max_items", n_items)


    def remove_all_features(self):
        """
        removes feature combination from feasible region of MIP
        :return:
        """
        mip = self.mip
        names = self.mip_indices['action_off_names']
        values = np.array(mip.solution.get_values(names))
        on_idx = np.flatnonzero(np.isclose(values, 0.0))
        mip.variables.set_lower_bounds([(names[j], 1.0) for j in on_idx])
        return


    def remove_feature_combination(self):
        """
        removes feature combination from feasible region of MIP
        :return:
        """

        mip = self.mip
        u_names = self.mip_indices['action_off_names']
        u = np.array(mip.solution.get_values(u_names))
        on_idx = np.isclose(u, 0.0)

        n = len(u_names)
        con_vals = np.ones(n, dtype = np.float_)
        con_vals[on_idx] = -1.0
        con_rhs = n - 1 - np.sum(on_idx)

        mip.linear_constraints.add(lin_expr = [SparsePair(ind = u_names, val = con_vals.tolist())],
                                   senses = ["L"],
                                   rhs = [float(con_rhs)])
        return


#### CPLEX


class _RecourseBuilderPyomo(RecourseBuilder):


    def __init__(self, action_set, x = None, **kwargs):

        self.built = False

        #setup the optimizer here:
        self._optimizer = SolverFactory['cbc']
        self._results = None

        #todo: Alex fill out these functions
        self._set_mip_time_limit = None #_set_mip_time_limit(self, mip, time_limit)
        self._set_mip_node_limit = None #_set_mip_node_limit(self, mip, node_limit)
        self._set_mip_display = None #_set_mip_display(self, mip, node_limit)

        super().__init__(action_set = action_set, x = x, **kwargs)

    #### building MIP ####
    def create_mip_model(self):
        """Build the model <b>object</b>."""

        def jk_init(m):
            return [(j, k) for j in m.J for k in m.K[j]]

        model = AbstractModel()
        model.J = Set()
        model.K = Set(model.J)
        model.JK = Set(initialize = jk_init, dimen = None)
        model.y_pred = Param()
        model.epsilon = Param()
        model.max_cost = Var()
        model.w = Param(model.J)
        model.a = Param(model.JK)
        model.c = Param(model.JK)
        model.u = Var(model.JK, within=Binary)

        # Make sure only one action is on at a time.
        def c1Rule(m, j):
            return sum([m.u[j, k] for k in m.K[j]]) == 1

        # 2.b: Action sets must flip the prediction of a linear classifier.
        def c2Rule(m):
            return sum((m.u[j, k] * m.a[j, k] * m.w[j]) for j, k in m.JK) >= -m.y_pred

        # instantiate max cost
        def maxcost_rule(m, j, k):
            return m.max_cost >= (m.u[j, k] * m.c[j, k])

        # Set up objective for total sum.
        def obj_rule_percentile(m):
            return sum(m.u[j, k] * m.c[j, k] for j, k in m.JK)

        # Set up objective for max cost.
        def obj_rule_max(m):
            return sum(m.epsilon * m.u[j, k] * m.c[j, k] for j, k in m.JK) + (1 - m.epsilon) * m.max_cost

        ## set up objective function.
        if self.mip_cost_type == "max":
            model.g = Objective(rule=obj_rule_max, sense=minimize)
            model.c3 = Constraint(model.JK, rule = maxcost_rule)
        else:
            model.g = Objective(rule=obj_rule_percentile, sense=minimize)

        ##
        model.c1 = Constraint(model.J, rule=c1Rule)
        model.c2 = Constraint(rule = c2Rule)
        return model


    def build_mip(self):

        self.model = self.create_mip_model()
        build_info, indices = self._get_mip_build_info()

        # custom changes to build info
        a = build_info['a']
        c = build_info['c']

        a_tuples = {}
        for i in range(len(a)):
            a_tuples[(i, 0)] = 0
            for j in range(len(a[i])):
                a_tuples[(i, j)] = a[i][j]

        c_tuples = {}
        for i in range(len(c)):
            c_tuples[(i, 0)] = 0
            for j in range(len(c[i])):
                c_tuples[(i, j)] = c[i][j]

        u_tuples = {}
        for i in range(len(c)):
            u_tuples[(i, 0)] = True
            for j in range(len(c[i])):
                u_tuples[(i, j)] = False

        epsilon = min(indices['cost_df']) / sum(indices['cost_ub'])
        data = {None: {
            'J':  {None: list(range(len(a)))},
            'K': {i: list(range(len(a[i]) or 1)) for i in range(len(a)) },
            'a': a_tuples,
            'c': c_tuples,
            'u': u_tuples,
            'w': {i: coef for i, coef in enumerate(self.coefficients)},
            'y_pred': {None: self.score()},
            'epsilon': {None: epsilon},
            'max_cost': {None: -1000}
            }}

        self._mip = self.model.create_instance(data)


    def _check_mip_build_info(self, build_info):
        ## TODO
        return True


    def _get_mip_build_info(self, cost_function_type = 'percentile', validate = True):
        build_info, indices = super()._get_mip_build_info(cost_function_type = cost_function_type, validate = validate)

        ## pyomo-specific processing
        c, a = [], []
        for name in self.action_set._names:
            c.append(build_info.get(name, {'costs': []})['costs'])
            a.append(build_info.get(name, {'actions': []})['actions'])

        output_build_info = {}
        output_build_info['a'] = a
        output_build_info['c'] = c
        output_build_info['epsilon'] = min(indices['cost_df']) / sum(indices['cost_ub'])
        return output_build_info, indices


    ##### solving MIP ####

    def solve_mip(self):
        self._results = self._optimizer.solve(self._mip)


    @property
    def solution_info(self):
        """
        fills out solution info
        :return:
        """
        results = self._results

        if results is None:
            raise ValueError('cannot access solution information before solving MIP')

        if results.solver.status != SolverStatus.ok:
            raise ValueError('solver status is not OK')

        if results.solver.TerminationCondition.infeasible:
            return self._mip_solution_info

        info = self._mip_solution_info
        assert results.solver.termination_condition == TerminationCondition.optimal

        mip = self._mip
        sol = {j: {'a': mip.a[j], 'u': mip.u[j](), 'c': mip.c[j]} for j in mip.JK}
        df = (pd.DataFrame.from_dict(sol, orient = "index").loc[lambda df: df['u'] == 1])

        info.update({
            'actions': df['a'].values,
            'costs': df['c'].values,
            'cost': mip.max_cost(),
            })

        return info

    #### Flipset methods (solver specific)
    def set_mip_min_items(self, n_items):
        """
        sets maximum number of non-zero elements in MIP (used by set_item_limits)
        :param n_items:
        :return:
        """
        #todo alex
        raise NotImplementedError()


    def set_mip_max_items(self, n_items):
        """
        sets maximum number of non-zero elements in MIP (used by set_item_limits)
        :param n_items:
        :return:
        """
        # todo alex
        raise NotImplementedError()


    def remove_all_features(self):
        """
        removes feature combination from feasible region of MIP
        :return:
        """
        #mip = self.mip
        #names = self.mip_indices['action_off_names']
        #values = np.array(mip.solution.get_values(names))
        #on_idx = np.flatnonzero(np.isclose(values, 0.0))
        #mip.variables.set_lower_bounds([(names[j], 1.0) for j in on_idx])
        # todo alex
        raise NotImplementedError()


    def remove_feature_combination(self):
        """
        removes feature combination from feasible region of MIP
        :return:
        """

        # mip = self.mip
        # u_names = self.mip_indices['action_off_names']
        # u = np.array(mip.solution.get_values(u_names))
        # on_idx = np.isclose(u, 0.0)
        #
        # n = len(u_names)
        # con_vals = np.ones(n, dtype = np.float_)
        # con_vals[on_idx] = -1.0
        # con_rhs = n - 1 - np.sum(on_idx)
        #
        # mip.linear_constraints.add(lin_expr = [SparsePair(ind = u_names, val = con_vals.tolist())],
        #                            senses = ["L"],
        #                            rhs = [float(con_rhs)])
        # todo alex
        raise NotImplementedError()




BUILDER_TO_SOLVER = {
    _SOLVER_TYPE_CPX: _RecourseBuilderCPX,
    _SOLVER_TYPE_CBC: _RecourseBuilderPyomo,
    }
