import asyncio
import logging

import async_timeout

logger = logging.getLogger(__name__)

from .sc2process import SC2Process
from .portconfig import Portconfig
from .client import Client
from .player import Human, Bot
from .data import Result, CreateGameError
from .game_state import GameState
from .protocol import ConnectionAlreadyClosed


async def _play_game_human(client, player_id, realtime, game_time_limit):
    while True:
        state = await client.observation()
        if client._game_result:
            return client._game_result[player_id]

        if game_time_limit and (state.observation.observation.game_loop * 0.725 * (1 / 16)) > game_time_limit:
            print(state.observation.game_loop, state.observation.game_loop * 0.14)
            return Result.Tie

        if not realtime:
            await client.step()


async def _play_game_ai(server, client, player_id, ai, realtime, step_time_limit, game_time_limit, reset, num_runs):
    game_data = await client.get_game_data()
    game_info = await client.get_game_info()

    ai._prepare_start(client, player_id, game_info, game_data)
    ai.on_start()
    ai.on_reset(None)

    iteration = 0
    run = 0
    num_wins = 0
    while True:
        state = await client.observation()
        gs = GameState(state.observation, game_data)

        if game_time_limit and (gs.game_loop * 0.725 * (1 / 16)) > game_time_limit:
            ai.on_end(Result.Tie)
            return num_wins

        ai._prepare_step(gs)
        if client._game_result or not ai.units or not ai.known_enemy_units:
            run += 1
            res = Result.Defeat

            if client._game_result:
                if client._game_result[player_id] == Result.Victory:
                    res = Result.Victory
                    num_wins += 1
            elif not ai.known_enemy_units:
                num_wins += 1
                res = Result.Victory

            if reset and run < num_runs:
                logger.debug("Result obtained. Reset...")
                await server.restart_game()
                ai.on_reset(res)
                client._game_result = None
                iteration = 0
                continue
            else:
                ai.on_end(res)
                return num_wins

        if iteration == 0:
            ai._prepare_first_step()

        logger.debug(f"Running AI step, realtime={realtime}")

        try:
            await ai.issue_events()
            if realtime:
                await ai.on_step(iteration)
            else:
                logger.debug(f"Running AI step, timeout={step_time_limit}")
                try:
                    async with async_timeout.timeout(step_time_limit):
                        await ai.on_step(iteration)
                except asyncio.TimeoutError:
                    logger.warning(f"Running AI step: out of time")
        except Exception as e:
            # NOTE: this message is caught by pytest suite
            logger.exception(f"AI step threw an error")  # DO NOT EDIT!
            logger.error(f"resigning due to previous error")
            ai.on_end(Result.Defeat)
            return num_wins

        logger.debug(f"Running AI step: done")

        if not realtime:
            if not client.in_game:  # Client left (resigned) the game
                ai.on_end(client._game_result[player_id])
                return num_wins

            await client.step()

        iteration += 1


async def _play_game(server, player, client, realtime, portconfig, step_time_limit=None, game_time_limit=None,
                     reset=False, num_runs=1):
    assert isinstance(realtime, bool), repr(realtime)

    player_id = await client.join_game(player.race, portconfig=portconfig)
    logging.info(f"Player id: {player_id}")

    if isinstance(player, Human):
        result = await _play_game_human(client, player_id, realtime, game_time_limit)
    else:
        result = await _play_game_ai(server, client, player_id, player.ai, realtime, step_time_limit, game_time_limit,
                                     reset, num_runs)

    logging.info(f"Result for player id: {player_id}: {result}")
    return result


async def _setup_host_game(server, map_settings, players, realtime):
    r = await server.create_game(map_settings, players, realtime)
    if r.create_game.HasField("error"):
        err = f"Could not create game: {CreateGameError(r.create_game.error)}"
        if r.create_game.HasField("error_details"):
            err += f": {r.create_game.error_details}"
        logger.critical(err)
        raise RuntimeError(err)

    return Client(server._ws)


async def _host_game(map_settings, players, realtime, portconfig=None, save_replay_as=None, step_time_limit=None,
                     game_time_limit=None, reset=False, num_runs=1, game_steps=8):
    assert len(players) > 0, "Can't create a game without players"

    assert any(isinstance(p, (Human, Bot)) for p in players)

    async with SC2Process() as server:
        await server.ping()

        client = await _setup_host_game(server, map_settings, players, realtime)
        client.game_step = game_steps

        try:
            result = await _play_game(server, players[0], client, realtime, portconfig, step_time_limit,
                                      game_time_limit, reset, num_runs)
            if save_replay_as is not None:
                await client.save_replay('Replays/' + save_replay_as)
            await client.leave()
            await client.quit()
        except ConnectionAlreadyClosed:
            logging.error(f"Connection was closed before the game ended")
            return None

        return result


async def _host_game_aiter(map_settings, players, realtime, portconfig=None, save_replay_as=None, step_time_limit=None,
                           game_time_limit=None, game_steps=8):
    assert len(players) > 0, "Can't create a game without players"

    assert any(isinstance(p, (Human, Bot)) for p in players)

    async with SC2Process() as server:
        run = 0
        while True:
            await server.ping()

            client = await _setup_host_game(server, map_settings, players, realtime)
            client.game_step = game_steps

            try:
                result = await _play_game(server, players[0], client, realtime, portconfig, step_time_limit,
                                          game_time_limit)
                run += 1
                if save_replay_as is not None:
                    await client.save_replay('Replays/' + str(run) + save_replay_as)
                await client.leave()
            except ConnectionAlreadyClosed:
                logging.error(f"Connection was closed before the game ended")
                return

            new_players = yield result
            if new_players is not None:
                players = new_players


def host_game_iter(*args, **kwargs):
    game = _host_game_aiter(*args, **kwargs)
    new_playerconfig = None
    while True:
        new_playerconfig = yield asyncio.get_event_loop().run_until_complete(game.asend(new_playerconfig))


async def _join_game(players, realtime, portconfig, save_replay_as=None, step_time_limit=None, game_time_limit=None,
                     game_steps=8, reset=False, num_runs=1):
    async with SC2Process() as server:
        await server.ping()

        client = Client(server._ws)
        client.game_step = game_steps

        try:
            result = await _play_game(server, players[1], client, realtime, portconfig, step_time_limit,
                                      game_time_limit, reset, num_runs)
            if save_replay_as is not None:
                await client.save_replay(save_replay_as)
            await client.leave()
            await client.quit()
        except ConnectionAlreadyClosed:
            logging.error(f"Connection was closed before the game ended")
            return None

        return result


def run_game(map_settings, players, **kwargs):
    if sum(isinstance(p, (Human, Bot)) for p in players) > 1:
        join_kwargs = {k: v for k, v in kwargs.items() if k != "save_replay_as"}

        portconfig = Portconfig()
        result = asyncio.get_event_loop().run_until_complete(asyncio.gather(
            _host_game(map_settings, players, **kwargs, portconfig=portconfig),
            _join_game(players, **join_kwargs, portconfig=portconfig)
        ))
    else:
        result = asyncio.get_event_loop().run_until_complete(
            _host_game(map_settings, players, **kwargs)
        )
    return result
