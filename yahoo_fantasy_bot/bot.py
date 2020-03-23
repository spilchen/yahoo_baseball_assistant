#!/bin/python

from yahoo_oauth import OAuth2
import yahoo_fantasy_api as yfa
from yahoo_fantasy_bot import roster, utils
import logging
import pickle
import os
import math
import datetime
import pandas as pd
import numpy as np
import importlib
import copy
import collections

LeagueStatics = collections.namedtuple("LeagueStatics",
                                       "pos ir_spots bn_spots settings cats")


class ScoreComparer:
    """
    Class that compares the scores of two lineups and computes whether it is
    *better* (in the fantasy sense)

    :param cfg: Configparser object
    :param scorer: Object that computes scores for the categories
    :param lg_lineups: All of the lineups in the league.  This is used to
        compute a standard deviation of all of the stat categories.
    """
    def __init__(self, cfg, scorer, lg_lineups):
        self.cfg = cfg
        self.scorer = scorer
        self.opp_sum = None
        self.stdev_cap = int(cfg['Scorer']['stdevCap'])
        self.stdevs = self._compute_agg(lg_lineups, 'std')

    def set_opponent(self, opp_sum):
        """
        Set the stat category totals for the opponent

        :param opp_sum: Sum of all of the categories of your opponent
        """
        self.opp_sum = opp_sum

    def compute_score(self, score_sum):
        """
        Calculate a lineup score by comparing it against the standard devs

        :param score_sum: Score summary of your lineup
        :return: Standard deviation score
        """
        assert(self.opp_sum is not None), "Must call set_opponent() first"
        assert(self.stdevs is not None)
        stddev_score = 0
        for (stat, c_opval) in self.opp_sum.items():
            assert(stat in score_sum)
            assert(stat in self.stdevs)
            c_myval = score_sum[stat]
            c_stdev = self.stdevs[stat].iloc(0)[0]
            v = (c_myval - c_opval) / c_stdev
            # Cap the value at a multiple of the standard deviation.  We do
            # this because we don't want to favour lineups that simply own
            # a category.  A few standard deviation is enough to provide a
            # cushion.  It also allows you to punt a category, if you don't
            # do well in a category, and you are going to lose, the down side
            # is capped.
            v = min(v, self.stdev_cap * c_stdev)
            if not self.scorer.is_highest_better(stat):
                v = v * -1
            stddev_score += v
        return stddev_score

    def print_stdev(self):
        print("Standard deviations for each category:")
        for cat, val in self.stdevs.iteritems():
            print("{} - {:.3f}".format(cat, val.iloc(0)[0]))
        print("")

    def _compute_agg(self, lineups, agg):
        """
        Compute an aggregation of each of the categories

        :param lineups: Lineups to compute the aggregation on
        :return: Aggregation compuation for each category
        :rtype: DataFrame
        """
        scores = pd.DataFrame()
        for lineup in lineups:
            if type(lineup) is pd.DataFrame:
                df = pd.DataFrame(data=lineup, columns=lineup.columns)
            else:
                df = pd.DataFrame(data=lineup, columns=lineup[0].index)
            score_sum = self.scorer.summarize(df)
            scores = scores.append(score_sum, ignore_index=True)
        return scores.agg([agg])


class ManagerBot:
    """A class that encapsulates an automated Yahoo! fantasy manager.
    """
    def __init__(self, cfg):
        self.logger = logging.getLogger()
        self.cfg = cfg
        self.sc = OAuth2(None, None, from_file=cfg['Connection']['oauthFile'])
        self.lg = yfa.League(self.sc, cfg['League']['id'])
        self.tm = self.lg.to_team(self.lg.team_key())
        self.tm_cache = utils.TeamCache(self.cfg, self.lg.team_key())
        self.lg_cache = utils.LeagueCache(self.cfg)
        self.load_league_statics()
        self.pred_bldr = None
        self.my_team_bldr = self._construct_roster_builder()
        self.ppool = None
        Scorer = self._get_scorer_class()
        self.scorer = Scorer(self.cfg)
        Display = self._get_display_class()
        self.display = Display(self.cfg)
        self.blacklist = self._load_blacklist()
        self.lineup = None
        self.bench = []
        self.injury_reserve = []
        self.opp_sum = None
        self.opp_team_name = None

        self.init_prediction_builder()
        self.score_comparer = ScoreComparer(self.cfg, self.scorer,
                                            self.fetch_league_lineups())
        self.fetch_player_pool()
        self.sync_lineup()
        self.pick_injury_reserve()
        self.auto_pick_opponent()

    def _load_blacklist(self):
        fn = self.tm_cache.blacklist_cache_file()
        if os.path.exists(fn):
            with open(fn, "rb") as f:
                blacklist = pickle.load(f)
        else:
            blacklist = []
        return blacklist

    def pick_bench(self):
        """Pick the bench spots based on the current roster."""
        bench = []
        if self.lg_statics.bn_spots == 0:
            return bench

        # We'll pick the bench spots by picking players not in your lineup or
        # IR but have the highest ownership %.
        lineup_names = [e['name'] for e in self.lineup] + \
            [e['name'] for e in self.injury_reserve]
        top_owners = self.ppool.sort_values(by=["percent_owned"],
                                            ascending=False)
        for plyr in top_owners.iterrows():
            p = plyr[1]
            if p['name'] not in lineup_names:
                self.logger.info("Adding {} to bench ({}%)...".format(
                    p['name'], p['percent_owned']))
                bench.append(p)
                if len(bench) == self.lg_statics.bn_spots:
                    break
        self.bench = bench

    def pick_injury_reserve(self):
        """Pick the injury reserve slots"""
        self.injury_reserve = []
        if self.lg_statics.ir_spots == 0:
            return

        ir = []
        roster = self._get_orig_roster()
        for plyr in roster:
            if plyr['status'] == 'IR':
                ir.append(plyr)
                for idx, lp in enumerate(self.lineup):
                    if lp['player_id'] == plyr['player_id']:
                        del self.lineup[idx]
                        break
                for idx, bp in enumerate(self.bench):
                    if bp['player_id'] == plyr['player_id']:
                        del self.bench[idx]
                        break

        if len(ir) < self.lg_statics.ir_spots:
            self.injury_reserve = ir
        else:
            assert(False), "Need to implement pruning of IR"

    def move_non_available_players(self):
        """Remove any player that has a status (e.g. DTD, SUSP, etc.).

        If the player is important enough, they will be added back to the bench
        pending the ownership percentage.
        """
        roster = self._get_orig_roster()
        for plyr in roster:
            if plyr['status'].strip() != '':
                for idx, lp in enumerate(self.lineup):
                    if lp['player_id'] == plyr['player_id']:
                        self.logger.info(
                            "Moving {} out of the starting lineup because "
                            "they are not available ({})".format(
                                plyr['name'], plyr['status']))
                        del self.lineup[idx]
                        break

    def _save_blacklist(self):
        fn = self.tm_cache.blacklist_cache_file()
        with open(fn, "wb") as f:
            pickle.dump(self.blacklist, f)

    def add_to_blacklist(self, plyr_name):
        self.blacklist.append(plyr_name)
        self._save_blacklist()

    def remove_from_blacklist(self, plyr_name):
        if plyr_name not in self.blacklist:
            return False
        else:
            self.blacklist.remove(plyr_name)
            self._save_blacklist()
            return True

    def get_blacklist(self):
        return self.blacklist

    def load_league_statics(self):
        """Load static settings for the league.

        These are settings that don't ever change.  These are cached to a file
        without any expiry.

        On exit, the self.lg_statics variable will be set.
        """
        def loader():
            pos = self.lg.positions()
            if "IR" in pos:
                ir_spots = pos['IR']['count']
            elif "IL" in pos:
                ir_spots = pos['IL']['count']
            else:
                ir_spots = 0
            bn_spots = pos['BN']['count'] if "BN" in pos else 0
            for del_pos in ['IR', 'IL', 'BN']:
                if del_pos in pos:
                    del pos[del_pos]
            return LeagueStatics(pos=pos,
                                 ir_spots=ir_spots,
                                 bn_spots=bn_spots,
                                 settings=self.lg.settings(),
                                 cats=self.lg.stat_categories())
        self.lg_statics = self.lg_cache.load_statics(loader)

    def init_prediction_builder(self):
        """Will load and return the prediction builder"""
        def loader():
            module = self._get_prediction_module()
            func = getattr(module,
                           self.cfg['Prediction']['builderClassLoader'])
            return func(self.lg, self.cfg)

        expiry = datetime.timedelta(
            minutes=int(self.cfg['Cache']['predictionBuilderExpiry']))
        self.pred_bldr = self.tm_cache.load_prediction_builder(expiry, loader)

    def save(self):
        self.tm_cache.refresh_prediction_builder(self.pred_bldr)

    def fetch_cur_lineup(self):
        """Fetch the current lineup as set in Yahoo!"""
        all_mine = self._get_orig_roster()
        pct_owned = self.lg.percent_owned([e['player_id'] for e in all_mine])
        for p, pct_own in zip(all_mine, pct_owned):
            if p['selected_position'] == 'BN' or \
                    p['selected_position'] == 'IR':
                p['selected_position'] = np.nan
            assert(pct_own['player_id'] == p['player_id'])
            p['percent_owned'] = pct_own['percent_owned']
        return all_mine

    def fetch_player_pool(self):
        """Build the roster pool of players"""
        if self.ppool is None:
            plyr_pool = self.fetch_free_agents() + self.fetch_cur_lineup()
            self.ppool = self._call_predict(plyr_pool, fail_on_missing=False)

    def fetch_free_agents(self):
        def loader():
            print("Fetching free agents from Yahoo!")
            self.logger.info("Fetching free agents")
            fa = self.lg.free_agents(None)
            self.logger.info(
                "Free agents fetch complete.  {} players in pool".
                format(len(fa)))
            return fa

        expiry = datetime.timedelta(
            minutes=int(self.cfg['Cache']['freeAgentExpiry']))
        return self.lg_cache.load_free_agents(expiry, loader)

    def fetch_league_lineups(self):
        def loader():
            self.logger.info("Fetching lineups for each team")
            lineups = []
            for tm_key in self.lg.teams().keys():
                tm = self.lg.to_team(tm_key)
                tm_roster = self._get_roster_for_team(tm)
                lineups.append(self._call_predict(tm_roster, fail_on_missing=True))
            self.logger.info("All lineups fetched.")
            return lineups

        return self.tm_cache.load_league_lineup(datetime.timedelta(days=5),
                                                loader)

    def invalidate_free_agents(self, plyrs):
        if os.path.exists(self.lg_cache.free_agents_cache_file()):
            with open(self.lg_cache.free_agents_cache_file(), "rb") as f:
                free_agents = pickle.load(f)

            plyr_ids = [e["player_id"] for e in plyrs]
            self.logger.info("Removing player IDs from free agent cache".
                             format(plyr_ids))
            new_players = [e for e in free_agents["payload"]
                           if e['player_id'] not in plyr_ids]
            free_agents['payload'] = new_players
            with open(self.lg_cache.free_agents_cache_file(), "wb") as f:
                pickle.dump(free_agents, f)

    def sum_opponent(self, opp_team_key):
        # Build up the predicted score of the opponent
        try:
            team_name = self._get_team_name(self.lg, opp_team_key)
        except LookupError:
            print("Not a valid team: {}:".format(opp_team_key))
            return(None, None)

        tm_roster = self._get_roster_for_team(self.lg.to_team(opp_team_key))
        opp_df = self._call_predict(tm_roster, fail_on_missing=True)
        opp_sum = self.scorer.summarize(opp_df)
        return (team_name, opp_sum)

    def _set_new_lineup_and_bench(self, new_lineup, frozen_bench):
        new_bench = frozen_bench
        new_plyr_ids = [e["player_id"] for e in new_lineup]
        for plyr in self.lineup:
            if plyr["player_id"] not in new_plyr_ids:
                new_bench.append(plyr)
        for plyr in self.bench:
            if plyr["player_id"] not in new_plyr_ids:
                new_bench.append(plyr)
        assert(len(new_bench) <= self.lg_statics.bn_spots)
        self.lineup = new_lineup
        self.bench = new_bench

    def fill_empty_spots_from_bench(self):
        if len(self.lineup) < self.my_team_bldr.max_players():
            # Only use bench players that are able to play
            avail_bench = []
            unavail_bench = []
            # SPILLY - TODO self.bench is empty when usin csv, broken for Yahoo
            for p in self.bench:
                if p.status == '':
                    avail_bench.append(p)
                else:
                    unavail_bench.append(p)
            if len(avail_bench) > 0:
                optimizer_func = self._get_lineup_optimizer_function()
                bench_df = pd.DataFrame(data=avail_bench,
                                        columns=avail_bench[0].index)
                new_lineup = optimizer_func(self.cfg, self.score_comparer,
                                            self.my_team_bldr, bench_df,
                                            self.lineup)
                if new_lineup:
                    self._set_new_lineup_and_bench(new_lineup.get_roster(), unavail_bench)

    def optimize_lineup_from_bench(self):
        """
        Optimizes your lineup using just your bench as potential player
        """
        if len(self.bench) == 0:
            return

        optimizer_func = self._get_lineup_optimizer_function()
        ppool = pd.DataFrame(data=self.bench, columns=self.bench[0].index)
        ldf = pd.DataFrame(data=self.lineup, columns=self.lineup[0].index)
        ppool = ppool.append(ldf, ignore_index=True, sort=False)
        ppool = ppool[ppool['status'] == '']
        new_lineup = optimizer_func(self.cfg, self.score_comparer,
                                    self.my_team_bldr, ppool, [])
        if new_lineup:
            self._set_new_lineup_and_bench(new_lineup.get_roster(), [])

    def fill_empty_spots(self):
        if len(self.lineup) < self.my_team_bldr.max_players():
            optimizer_func = self._get_lineup_optimizer_function()
            new_lineup = optimizer_func(self.cfg, self.score_comparer,
                                        self.my_team_bldr,
                                        self._get_filtered_pool(), self.lineup)
            if new_lineup:
                self.lineup = new_lineup.get_roster()

    def print_roster(self):
        self.display.printRoster(self.lineup, self.bench, self.injury_reserve)

    def sync_lineup(self):
        """Reset the local lineup to the one that is set in Yahoo!"""
        yahoo_roster = self._get_orig_roster()
        roster_ids = [{'player_id': e['player_id'], 'name': e['name']}
                      for e in yahoo_roster]
        bench_ids = [e['player_id'] for e in yahoo_roster
                     if e['selected_position'] == 'BN']
        ir_ids = [e['player_id'] for e in yahoo_roster
                  if (e['selected_position'] == 'DL' or
                      e['selected_position'] == 'IR')]
        sel_plyrs = self.pred_bldr.select_players(roster_ids)
        lineup = []
        bench = []
        ir = []
        for plyr in sel_plyrs.iterrows():
            if plyr[1]['player_id'] in bench_ids:
                bench.append(plyr[1])
            elif plyr[1]['player_id'] in ir_ids:
                ir.append(plyr[1])
            else:
                lineup.append(plyr[1])
        self.lineup = lineup
        self.bench = bench
        self.injury_reserve = ir

    def _get_filtered_pool(self):
        """
        Get a list of players from the pool filtered on common criteria

        :return: Player pool
        :rtype: DataFrame
        """
        avail_plyrs = self.ppool[~self.ppool['name'].isin(self.blacklist)]
        avail_plyrs = avail_plyrs[avail_plyrs['percent_owned'] > 10]
        return avail_plyrs[avail_plyrs['status'] == '']

    def optimize_lineup_from_free_agents(self):
        """
        Optimize your lineup using all of your players plus free agents

        :return: True if a new lineup was selected
        """
        optimizer_func = self._get_lineup_optimizer_function()

        locked_plyrs = []
        thres = int(self.cfg['LineupOptimizer']['lockPlayersAbovePctOwn'])
        for plyr in self.lineup:
            if plyr['percent_owned'] >= thres:
                locked_plyrs.append(plyr)

        best_lineup = optimizer_func(self.cfg, self.score_comparer,
                                     self.my_team_bldr,
                                     self._get_filtered_pool(), locked_plyrs)
        if best_lineup:
            self.lineup = copy.deepcopy(best_lineup.get_roster())
        return best_lineup is not None

    def show_score(self):
        if self.opp_sum is None:
            raise RuntimeError("No opponent selected")

        self.score_comparer.print_stdev()

        df = pd.DataFrame(data=self.lineup, columns=self.lineup[0].index)
        my_sum = self.scorer.summarize(df)
        score = self.score_comparer.compute_score(my_sum)
        print("Against '{}' your roster has a score of: {}".
              format(self.opp_team_name, score))
        print("")
        for stat in my_sum.keys():
            if stat in ["ERA", "WHIP"]:
                if math.isclose(my_sum[stat], self.opp_sum[stat]):
                    my_win = "="
                    opp_win = "="
                elif my_sum[stat] < self.opp_sum[stat]:
                    my_win = "*"
                    opp_win = ""
                else:
                    my_win = ""
                    opp_win = "*"
            else:
                if math.isclose(my_sum[stat], self.opp_sum[stat]):
                    my_win = "="
                    opp_win = "="
                elif my_sum[stat] > self.opp_sum[stat]:
                    my_win = "*"
                    opp_win = ""
                else:
                    my_win = ""
                    opp_win = "*"
            print("{:5} {:2.3f} {:1} v.s. {:2.3f} {:2}".format(
                stat, my_sum[stat], my_win, self.opp_sum[stat], opp_win))

    def list_players(self, pos):
        self.display.printListPlayerHeading(pos)

        for plyr in self.ppool.iterrows():
            if pos in plyr[1]['eligible_positions']:
                self.display.printPlayer(pos, plyr)

    def find_in_lineup(self, name):
        for idx, p in enumerate(self.lineup):
            if p['name'] == name:
                return idx
        raise LookupError("Could not find player: " + name)

    def swap_player(self, plyr_name_del, plyr_name_add):
        if plyr_name_add:
            plyr_add_df = self.ppool[self.ppool['name'] == plyr_name_add]
            if(len(plyr_add_df.index) == 0):
                raise LookupError("Could not find player in pool: {}".format(
                    plyr_name_add))
            if(len(plyr_add_df.index) > 1):
                raise LookupError("Found more than one player!: {}".format(
                    plyr_name_add))
            plyr_add = plyr_add_df.iloc(0)[0]
        else:
            plyr_add = None

        idx = self.find_in_lineup(plyr_name_del)
        plyr_del = self.lineup[idx]
        assert(type(plyr_del.selected_position) == str)
        if plyr_add and plyr_del.selected_position not in \
                plyr_add['eligible_positions']:
            raise ValueError("Position {} is not a valid position for {}: {}".
                             format(plyr_del.selected_position,
                                    plyr_add['name'],
                                    plyr_add['eligible_positions']))

        if plyr_add:
            plyr_add['selected_position'] = plyr_del['selected_position']
        plyr_del['selected_position'] = np.nan
        if plyr_add:
            self.lineup[idx] = plyr_add
        else:
            del(self.lineup[idx])
        self.pick_bench()

    def apply_roster_moves(self, dry_run, prompt):
        """Make roster changes with Yahoo!

        :param dry_run: Just enumerate the roster moves but don't apply yet
        :param prompt: Prompt for yes before proceeding
        :type dry_run: bool
        """
        roster_chg = RosterChanger(self.lg, dry_run, self._get_orig_roster(),
                                   self.lineup, self.bench,
                                   self.injury_reserve, prompt)
        roster_chg.apply()

        # Change the free agent cache to remove the players we added
        if not dry_run:
            adds = roster_chg.get_adds_completed()
            self.invalidate_free_agents(adds)

    def pick_opponent(self, opp_team_key):
        (self.opp_team_name, self.opp_sum) = self.sum_opponent(opp_team_key)
        self.score_comparer.set_opponent(self.opp_sum)

    def auto_pick_opponent(self):
        edit_wk = self.lg.current_week()
        (wk_start, wk_end) = self.lg.week_date_range(edit_wk)
        edit_date = self.lg.edit_date()
        if edit_date > wk_end:
            edit_wk += 1

        try:
            opp_team_key = self.tm.matchup(edit_wk)
        except RuntimeError:
            self.logger.info("Could not find opponent.  Picking ourselves...")
            opp_team_key = self.lg.team_key()

        self.pick_opponent(opp_team_key)

    def evaluate_trades(self, dry_run, verbose, prompt=False):
        """
        Find any proposed trades against my team and evaluate them.

        :param dry_run: True if we will evaluate the trades but not send the
            accept or reject through to Yahoo.
        :param verbose: If true, we will print details to the console
        :return: Number of trades evaluated
        """
        trades = self.tm.proposed_trades()
        self.logger.info(trades)
        # We don't evaluate trades that we sent out.
        actionable_trades = [tr for tr in trades
                             if tr['tradee_team_key'] == self.tm.team_key]
        self.logger.info(actionable_trades)

        if len(actionable_trades) > 0:
            for trade in actionable_trades:
                ev = self._evaluate_trade(trade)
                if verbose:
                    self._print_trade(trade, ev)
                self.logger.warn("Accept={}    {}".format(ev, trade))
                if not dry_run:
                    if prompt:
                        p = input("Enter 'yes' to proceed?")
                        proceed = p.lower() == 'yes'
                    else:
                        proceed = True

                    if proceed:
                        if ev:
                            self.tm.accept_trade(trade['transaction_key'])
                        else:
                            self.tm.reject_trade(trade['transaction_key'])
        return len(actionable_trades)

    def _evaluate_trade(self, trade):
        """
        Evaluate a single trade

        :return: True if trade should be accepted.  False otherwise.
        """
        if self.cfg['Trade'].getboolean('autoReject'):
            return False
        else:
            assert(False), "No support for evaluating trades"

    def _print_trade(self, trade, ev):
        print("\nSending")
        for plyr in trade['trader_players']:
            print("  {}".format(plyr['name']))
        print("for your")
        for plyr in trade['tradee_players']:
            print("  {}".format(plyr['name']))
        print("\nTrade should be {}".format("accepted" if ev else "rejected"))

    def _get_team_name(self, lg, team_key):
        teams = lg.teams()
        if team_key in teams:
            return teams[team_key]['name']
        else:
            raise LookupError("Could not find team for team key: {}".format(
                team_key))

    def _get_prediction_module(self):
        """Return the module to use for the prediction builder.

        The details about what prediction builder is taken from the config.
        """
        return importlib.import_module(
            self.cfg['Prediction']['builderModule'],
            package=self.cfg['Prediction']['builderPackage'])

    def _get_scorer_class(self):
        module = importlib.import_module(
            self.cfg['Scorer']['module'],
            package=self.cfg['Scorer']['package'])
        return getattr(module, self.cfg['Scorer']['class'])

    def _get_display_class(self):
        module = importlib.import_module(
            self.cfg['Display']['module'],
            package=self.cfg['Display']['package'])
        return getattr(module, self.cfg['Display']['class'])

    def _get_lineup_optimizer_function(self):
        """Return the function used to optimize a lineup.

        The config file is used to determine the appropriate function.
        """
        module = importlib.import_module(
            self.cfg['LineupOptimizer']['module'],
            package=self.cfg['LineupOptimizer']['package'])
        return getattr(module, self.cfg['LineupOptimizer']['function'])

    def _construct_roster_builder(self):
        pos_list = []
        for pos_name, pos_detail in self.lg_statics.pos.items():
            for _ in range(int(pos_detail['count'])):
                pos_list.append(pos_name)
        return roster.Builder(pos_list)

    def _get_position_types(self):
        settings = self.lg.settings()
        position_types = {'mlb': ['B', 'P'], 'nhl': ['G', 'P']}
        return position_types[settings['game_code']]

    def _is_predicted_stat(self, stat):
        return stat in self.cfg['League']['predictedStatCategories'].split(',')

    def _get_orig_roster(self):
        return self.lg.to_team(self.lg.team_key()).roster(
            day=self.lg.edit_date())

    def _call_predict(self, plyrs, fail_on_missing):
            kwargs = {
                k: v for k, v in self.cfg['PredictionNamedArguments'].items()
            }
            return self.pred_bldr.predict(
                plyrs, fail_on_missing=fail_on_missing,
                **kwargs)

    def _get_roster_for_team(self, team):
        """Get all the players that are active for a given team

        :param team: Team to get roster for
        :type team: yahoo_fantasy_api.Team
        :return: Roster of players
        :rtype: list
        """
        week = self.lg.current_week() + 1
        if week > self.lg.end_week():
            raise RuntimeError("Season over no more weeks to predict")
        full_roster = team.roster(week)
        return [e for e in full_roster
                if e["selected_position"] not in ["IR", "BN", "IL"]]


class RosterChanger:
    def __init__(self, lg, dry_run, orig_roster, lineup, bench,
                 injury_reserve, prompt):
        self.lg = lg
        self.tm = lg.to_team(lg.team_key())
        self.dry_run = dry_run
        self.prompt = prompt
        self.orig_roster = orig_roster
        self.lineup = lineup
        self.bench = bench
        self.injury_reserve = injury_reserve
        self.orig_roster_ids = [e['player_id'] for e in orig_roster]
        self.new_roster_ids = [e['player_id'] for e in lineup] + \
            [e['player_id'] for e in bench] + \
            [e['player_id'] for e in injury_reserve]
        self.adds = []
        self.drops = []
        self.adds_completed = []

    def _continue_with_yahoo(self):
        if self.dry_run:
            return False
        elif not self.prompt:
            return True
        else:
            p = input("Enter 'yes' to proceed?")
            return p.lower() == 'yes'

    def apply(self):
        self._calc_player_drops()
        self._calc_player_adds()
        # Need to drop players first in case the person on IR isn't dropped
        self._apply_player_drops()
        self._apply_ir_moves()
        self._apply_player_adds_and_drops()
        self._apply_position_selector()

    def get_adds_completed(self):
        return self.adds_completed

    def _calc_player_drops(self):
        self.drops = []
        for plyr in self.orig_roster:
            if plyr['player_id'] not in self.new_roster_ids:
                self.drops.append(plyr)

    def _calc_player_adds(self):
        self.adds = []
        for plyr in self.lineup + self.bench:
            if plyr['player_id'] not in self.orig_roster_ids:
                self.adds.append(plyr)

    def _apply_player_drops(self):
        while len(self.drops) > len(self.adds):
            plyr = self.drops.pop()
            print("Drop " + plyr['name'])
            if self._continue_with_yahoo():
                self.tm.drop_player(plyr['player_id'])

    def _apply_player_adds_and_drops(self):
        while len(self.drops) != len(self.adds):
            if len(self.drops) > len(self.adds):
                plyr = self.drops.pop()
                print("Drop " + plyr['name'])
                if self._continue_with_yahoo():
                    self.tm.drop_player(plyr['player_id'])
            else:
                plyr = self.adds.pop()
                self.adds_completed.append(plyr)
                print("Add " + plyr['name'])
                if self._continue_with_yahoo():
                    self.tm.add_player(plyr['player_id'])

        for add_plyr, drop_plyr in zip(self.adds, self.drops):
            self.adds_completed.append(add_plyr)
            print("Add {} and drop {}".format(add_plyr['name'],
                                              drop_plyr['name']))
            if self._continue_with_yahoo():
                self.tm.add_and_drop_players(add_plyr['player_id'],
                                             drop_plyr['player_id'])

    def _apply_one_player_drop(self):
        if len(self.drops) > 0:
            plyr = self.drops.pop()
            print("Drop " + plyr['name'])
            if self._continue_with_yahoo():
                self.tm.drop_player(plyr['player_id'])

    def _apply_ir_moves(self):
        orig_ir = [e for e in self.orig_roster
                   if e['selected_position'] == 'IR']
        new_ir_ids = [e['player_id'] for e in self.injury_reserve]
        pos_change = []
        num_drops = 0
        for plyr in orig_ir:
            if plyr['player_id'] in self.new_roster_ids and \
                    plyr['player_id'] not in new_ir_ids:
                pos_change.append({'player_id': plyr['player_id'],
                                   'selected_position': 'BN',
                                   'name': plyr['name']})
                num_drops += 1

        for plyr in self.injury_reserve:
            assert(plyr['player_id'] in self.orig_roster_ids)
            pos_change.append({'player_id': plyr['player_id'],
                               'selected_position': 'IR',
                               'name': plyr['name']})
            num_drops -= 1

        # Prior to changing any of the IR spots, we may need to drop players.
        # The number has been precalculated in the above loops.  Basically the
        # different in the number of players moving out of IR v.s. moving into
        # IR.
        for _ in range(num_drops):
            self._apply_one_player_drop()

        for plyr in pos_change:
            print("Move {} to {}".format(plyr['name'],
                                         plyr['selected_position']))
        if len(pos_change) > 0 and self._continue_with_yahoo():
            self.tm.change_positions(self.lg.edit_date(), pos_change)

    def _apply_position_selector(self):
        pos_change = []
        for plyr in self.lineup:
            pos_change.append({'player_id': plyr['player_id'],
                               'selected_position': plyr['selected_position']})
            print("Move {} to {}".format(plyr['name'],
                                         plyr['selected_position']))
        for plyr in self.bench:
            pos_change.append({'player_id': plyr['player_id'],
                               'selected_position': 'BN'})
            print("Move {} to BN".format(plyr['name']))

        if self._continue_with_yahoo():
            self.tm.change_positions(self.lg.edit_date(), pos_change)
