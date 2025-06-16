from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import uuid
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'werewolf_game_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

class WerewolfGame:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {}
        self.game_state = "waiting"
        self.current_phase = "waiting"
        self.day_count = 0
        self.votes = {}
        self.night_actions = {}
        self.game_log = []
        self.alive_players = set()
        self.host_id = None
        self.custom_roles = []
        self.revenge_waiting = None
        self.night_confirmations = set()
        self.day_confirmations = set()
        self.voting_confirmations = set()
        self.last_wolf_target = None  # 被狼人刀的目標

        self.all_roles = {
            'villager': {'name': '村民', 'team': 'village', 'ability': None, 'description': '普通村民，沒有特殊能力'},
            'werewolf': {'name': '狼人', 'team': 'werewolf', 'ability': 'kill', 'description': '每晚可以殺死一名玩家'},
            'seer': {'name': '預言家', 'team': 'village', 'ability': 'check', 'description': '每晚可以查驗一名玩家身份'},
            'witch': {'name': '女巫', 'team': 'village', 'ability': 'potion', 'description': '有解藥和毒藥各一瓶'},
            'hunter': {'name': '獵人', 'team': 'village', 'ability': 'shoot', 'description': '死亡時可以開槍帶走一名玩家'},
            'guard': {'name': '守衛', 'team': 'village', 'ability': 'protect', 'description': '每晚可以守護一名玩家'},
            'wolf_king': {'name': '狼王', 'team': 'werewolf', 'ability': 'kill_on_death', 'description': '死亡時可以帶走一名玩家'},
            'white_wolf_king': {'name': '白狼王', 'team': 'werewolf', 'ability': 'self_destruct', 'description': '白天可以自爆帶走一名玩家'},
            'knight': {'name': '騎士', 'team': 'village', 'ability': 'duel', 'description': '白天可以挑戰一名玩家決鬥'},
            'idiot': {'name': '白痴', 'team': 'village', 'ability': 'survive_vote', 'description': '被投票出局時不會死亡，但失去投票權'},
            'magician': {'name': '魔術師', 'team': 'village', 'ability': 'exchange', 'description': '每晚可以交換兩名玩家的身份'},
            'little_girl': {'name': '小女孩', 'team': 'village', 'ability': 'peek', 'description': '夜晚可以偷看狼人行動'}
        }
        self.witch_potions = {}

    def get_wolf_leader(self):
        alive_wolves = [pid for pid in self.alive_players if self.players[pid]['role'] in ['werewolf', 'wolf_king', 'white_wolf_king']]
        return min(alive_wolves) if alive_wolves else None

    def add_player(self, player_name, socket_id):
        player_id = str(uuid.uuid4())
        self.players[player_id] = {
            'name': player_name,
            'socket_id': socket_id,
            'role': None,
            'alive': True,
            'voted_for': None,
            'can_vote': True,
            'special_status': {}
        }
        if self.host_id is None:
            self.host_id = player_id
        return player_id

    def remove_player(self, player_id):
        if player_id in self.players:
            del self.players[player_id]
            self.alive_players.discard(player_id)
            if player_id == self.host_id and self.players:
                self.host_id = next(iter(self.players.keys()))

    def set_custom_roles(self, roles_config):
        self.custom_roles = roles_config
        return True

    def start_game(self):
        if len(self.players) < 4:
            return False, "至少需要4名玩家"
        if not self.custom_roles:
            return False, "請先設置角色配置"
        total_roles = sum(role['count'] for role in self.custom_roles)
        if total_roles != len(self.players):
            return False, f"角色總數({total_roles})與玩家人數({len(self.players)})不匹配"
        role_list = []
        for role_config in self.custom_roles:
            role_list.extend([role_config['role']] * role_config['count'])
        random.shuffle(role_list)
        player_ids = list(self.players.keys())
        for i, player_id in enumerate(player_ids):
            self.players[player_id]['role'] = role_list[i]
            if role_list[i] == 'witch':
                self.witch_potions[player_id] = {'antidote': True, 'poison': True}
        self.alive_players = set(self.players.keys())
        self.game_state = "night"
        self.day_count = 1
        self.night_confirmations = set()
        self.day_confirmations = set()
        self.add_log("遊戲開始！第1個夜晚降臨...")
        return True, "遊戲開始成功"

    def get_player_role_info(self, player_id):
        if player_id not in self.players:
            return None
        player = self.players[player_id]
        role_info = self.all_roles[player['role']]
        result = {
            'role': role_info['name'],
            'role_key': player['role'],
            'team': role_info['team'],
            'ability': role_info['ability'],
            'description': role_info['description']
        }
        if role_info['team'] == 'werewolf':
            teammates = []
            for pid, p in self.players.items():
                if (self.all_roles[p['role']]['team'] == 'werewolf' and pid != player_id and p['alive']):
                    teammates.append({'id': pid, 'name': p['name'], 'role': self.all_roles[p['role']]['name']})
            result['teammates'] = teammates
            result['wolf_leader'] = (player_id == self.get_wolf_leader())
        if player['role'] == 'witch' and player_id in self.witch_potions:
            result['potions'] = self.witch_potions[player_id]
        return result

    def night_action(self, player_id, action_type, target_id=None, additional_target=None):
        if self.game_state != "night":
            return False, "現在不是夜晚階段"
        if player_id not in self.alive_players:
            return False, "死者無法行動"
        player = self.players[player_id]
        role = player['role']
        role_ability = self.all_roles[role]['ability']
        # 狼人殺人只允許首狼
        if role_ability == 'kill':
            if player_id != self.get_wolf_leader():
                return False, "只有狼人代表可以決定殺人目標"
        if action_type == "self_destruct" and role == "white_wolf_king":
            return False, "白狼王只能在白天自爆"
        if not self._validate_night_action(player_id, action_type, target_id, additional_target):
            return False, "無效的行動"
        self.night_actions[player_id] = {
            'action': action_type,
            'target': target_id,
            'additional_target': additional_target
        }
        return True, f"{action_type}行動已記錄"

    def _validate_night_action(self, player_id, action_type, target_id, additional_target):
        player = self.players[player_id]
        role = player['role']
        role_ability = self.all_roles[role]['ability']
        valid_actions = {
            'kill': ['kill'],
            'check': ['check'],
            'protect': ['protect'],
            'poison': ['potion'],
            'antidote': ['potion'],
            'self_destruct': [],  # 白狼王夜晚不可自爆
            'exchange': ['exchange'],
            'peek': ['peek']
        }
        if action_type not in valid_actions.get(role_ability, []):
            return False
        if action_type in ['poison', 'antidote'] and role == 'witch':
            potion_type = 'poison' if action_type == 'poison' else 'antidote'
            if not self.witch_potions[player_id][potion_type]:
                return False
        if action_type == 'exchange' and (not target_id or not additional_target):
            return False
        return True

    def confirm_night(self, player_id):
        self.night_confirmations.add(player_id)
        if self.night_confirmations >= self.alive_players:
            self.night_confirmations.clear()
            return True
        return False

    def confirm_day(self, player_id):
        self.day_confirmations.add(player_id)
        if self.day_confirmations >= self.alive_players:
            self.day_confirmations.clear()
            return True
        return False

    def process_night(self):
        if self.game_state != "night":
            return False, "不是夜晚階段"
        killed = set()
        protected = set()
        results = []
        # 處理守護
        for player_id, action in self.night_actions.items():
            if action['action'] == 'protect':
                protected.add(action['target'])
        # 處理狼人殺人
        werewolf_targets = []
        for player_id, action in self.night_actions.items():
            if (action['action'] == 'kill' and self.all_roles[self.players[player_id]['role']]['team'] == 'werewolf'):
                werewolf_targets.append(action['target'])
        wolf_target = None
        if werewolf_targets:
            wolf_target = werewolf_targets[0]
            if wolf_target not in protected:
                killed.add(wolf_target)
        self.last_wolf_target = wolf_target
        # 女巫夜晚得知誰被殺
        for pid, player in self.players.items():
            if player['role'] == 'witch' and player['alive']:
                socketio.emit('witch_night_info', {
                    'killed_player_id': wolf_target,
                    'killed_player_name': self.players[wolf_target]['name'] if wolf_target else None
                }, room=player['socket_id'])
        # 女巫毒殺
        for player_id, action in self.night_actions.items():
            if action['action'] == 'poison':
                killed.add(action['target'])
                self.witch_potions[player_id]['poison'] = False
        # 女巫解藥
        for player_id, action in self.night_actions.items():
            if action['action'] == 'antidote':
                if action['target'] in killed:
                    killed.remove(action['target'])
                self.witch_potions[player_id]['antidote'] = False
        # 預言家查驗
        for player_id, action in self.night_actions.items():
            if action['action'] == 'check':
                target = action['target']
                target_role = self.players[target]['role']
                is_werewolf = self.all_roles[target_role]['team'] == 'werewolf'
                results.append({
                    'player_id': player_id,
                    'type': 'check',
                    'target_name': self.players[target]['name'],
                    'result': '狼人' if is_werewolf else '好人'
                })
        # 魔術師交換
        for player_id, action in self.night_actions.items():
            if action['action'] == 'exchange':
                target1 = action['target']
                target2 = action['additional_target']
                role1 = self.players[target1]['role']
                role2 = self.players[target2]['role']
                self.players[target1]['role'] = role2
                self.players[target2]['role'] = role1
                results.append({'player_id': player_id, 'type': 'exchange', 'message': f"已交換 {self.players[target1]['name']} 和 {self.players[target2]['name']} 的身份"})
        # 狼王夜間被殺
        wolf_king_now = None
        for player_id in list(killed):
            if self.players[player_id]['role'] == 'wolf_king':
                wolf_king_now = player_id
                break
        if wolf_king_now:
            self.players[wolf_king_now]['alive'] = False
            self.alive_players.discard(wolf_king_now)
            self.revenge_waiting = (wolf_king_now, 'night')
            self.game_state = "wolf_king_revenge"
            self.add_log(f"{self.players[wolf_king_now]['name']}（狼王）死亡，等待其帶走一人")
            return True, results
        for player_id in killed:
            if player_id in self.players:
                self.players[player_id]['alive'] = False
                self.alive_players.discard(player_id)
        if killed:
            killed_names = [self.players[pid]['name'] for pid in killed if pid in self.players]
            self.add_log(f"夜晚結束，{', '.join(killed_names)} 死亡")
        else:
            self.add_log("夜晚結束，平安夜")
        self.game_state = "day"
        self.night_actions = {}
        winner = self.check_winner()
        if winner:
            self.game_state = "ended"
            self.add_log(f"遊戲結束！{winner}勝利！")
        return True, results

    def day_action(self, player_id, action_type, target_id=None):
        if self.game_state != "day":
            return False, "現在不是白天階段"
        if player_id not in self.alive_players:
            return False, "死者無法行動"
        player = self.players[player_id]
        if action_type == 'duel' and player['role'] == 'knight':
            if target_id not in self.alive_players:
                return False, "目標已死亡"
            target = self.players[target_id]
            target_is_werewolf = self.all_roles[target['role']]['team'] == 'werewolf'
            if target_is_werewolf:
                self.players[target_id]['alive'] = False
                self.alive_players.discard(target_id)
                self.add_log(f"騎士 {player['name']} 決鬥成功，{target['name']} 死亡")
            else:
                self.players[player_id]['alive'] = False
                self.alive_players.discard(player_id)
                self.add_log(f"騎士 {player['name']} 決鬥失敗，自己死亡")
            return True, "決鬥完成"
        if action_type == 'self_destruct' and player['role'] == 'white_wolf_king':
            if target_id not in self.alive_players:
                return False, "目標已死亡"
            self.players[target_id]['alive'] = False
            self.alive_players.discard(target_id)
            self.players[player_id]['alive'] = False
            self.alive_players.discard(player_id)
            self.add_log(f"白狼王 {player['name']} 白天自爆，帶走了 {self.players[target_id]['name']}")
            winner = self.check_winner()
            if winner:
                self.game_state = "ended"
                self.add_log(f"遊戲結束！{winner}勝利！")
            return True, "自爆完成"
        return False, "無效的行動"

    def confirm_vote(self, player_id):
        self.voting_confirmations.add(player_id)
        if self.voting_confirmations >= self.alive_players:
            self.voting_confirmations.clear()
            return True
        return False

    def vote(self, player_id, target_id):
        if self.game_state != "voting":
            return False, "現在不是投票階段"
        if player_id not in self.alive_players:
            return False, "死者無法投票"
        if not self.players[player_id]['can_vote']:
            return False, "你已失去投票權"
        self.votes[player_id] = target_id
        return True, "投票成功"

    def process_vote(self):
        if not self.votes:
            self.add_log("沒有人投票，進入夜晚")
            self.game_state = "night"
            self.day_count += 1
            return True, "投票結束"
        vote_count = {}
        for voter, target in self.votes.items():
            vote_count[target] = vote_count.get(target, 0) + 1
        max_votes = max(vote_count.values())
        candidates = [pid for pid, count in vote_count.items() if count == max_votes]
        if len(candidates) == 1:
            eliminated = candidates[0]
            eliminated_player = self.players[eliminated]
            eliminated_role = self.all_roles[eliminated_player['role']]['name']
            if eliminated_player['role'] == 'idiot':
                eliminated_player['can_vote'] = False
                self.add_log(f"{eliminated_player['name']} (白痴) 被投票出局但沒有死亡，失去投票權")
            elif eliminated_player['role'] == 'wolf_king':
                eliminated_player['alive'] = False
                self.alive_players.discard(eliminated)
                self.revenge_waiting = (eliminated, 'day')
                self.game_state = "wolf_king_revenge"
                self.add_log(f"{eliminated_player['name']} (狼王) 被投票出局，等待其帶走一人")
                return True, "投票結束"
            else:
                self.players[eliminated]['alive'] = False
                self.alive_players.discard(eliminated)
                self.add_log(f"{eliminated_player['name']} ({eliminated_role}) 被投票出局")
        else:
            self.add_log("投票平票，沒有人出局")
        self.votes = {}
        self.game_state = "night"
        self.day_count += 1
        self.add_log(f"第{self.day_count}個夜晚降臨...")
        winner = self.check_winner()
        if winner:
            self.game_state = "ended"
            self.add_log(f"遊戲結束！{winner}勝利！")
        return True, "投票結束"

    def wolf_king_revenge(self, revenge_target_id):
        wolf_king_id, _ = self.revenge_waiting
        if revenge_target_id in self.alive_players:
            self.players[revenge_target_id]['alive'] = False
            self.alive_players.discard(revenge_target_id)
            self.add_log(f"狼王帶走了 {self.players[revenge_target_id]['name']}")
        self.revenge_waiting = None
        winner = self.check_winner()
        if winner:
            self.game_state = "ended"
            self.add_log(f"遊戲結束！{winner}勝利！")
        else:
            if self.game_state == "wolf_king_revenge":
                _, cause = wolf_king_id, _
                self.game_state = "day" if cause == 'day' else "night"

    def check_winner(self):
        if not self.alive_players:
            return "平局"
        alive_roles = [self.players[pid]['role'] for pid in self.alive_players]
        alive_teams = [self.all_roles[role]['team'] for role in alive_roles]
        werewolf_count = alive_teams.count('werewolf')
        village_count = alive_teams.count('village')
        if werewolf_count == 0:
            return "好人陣營"
        elif werewolf_count >= village_count:
            return "狼人陣營"
        return None

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.game_log.append(f"[{timestamp}] {message}")

    def get_game_state(self, player_id=None):
        state = {
            'game_state': self.game_state,
            'day_count': self.day_count,
            'players': [],
            'game_log': self.game_log[-10:],
            'is_host': player_id == self.host_id if player_id else False
        }
        for pid, player in self.players.items():
            player_info = {
                'id': pid,
                'name': player['name'],
                'alive': player['alive'],
                'can_vote': player.get('can_vote', True)
            }
            if self.game_state == "ended" or pid == player_id:
                if player['role'] and player['role'] in self.all_roles:
                    player_info['role'] = self.all_roles[player['role']]['name']
                    player_info['team'] = self.all_roles[player['role']]['team']
            state['players'].append(player_info)
        if self.game_state == "wolf_king_revenge" and self.revenge_waiting:
            state['revenge_waiting'] = {
                'wolf_king_id': self.revenge_waiting[0],
                'wolf_king_name': self.players[self.revenge_waiting[0]]['name']
            }
        return state

games = {}

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

def join_wolf_room(game, room_id):
    wolf_room = room_id + "_wolves"
    for pid, player in game.players.items():
        if player['alive'] and game.all_roles[player['role']]['team'] == 'werewolf':
            socketio.server.enter_room(player['socket_id'], wolf_room)

@socketio.on('create_room')
def handle_create_room(data):
    room_id = str(uuid.uuid4())[:8]
    games[room_id] = WerewolfGame(room_id)
    player_id = games[room_id].add_player(data['player_name'], request.sid)
    join_room(room_id)
    emit('room_created', {
        'room_id': room_id,
        'player_id': player_id,
        'game_state': games[room_id].get_game_state(player_id)
    })

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data['room_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    if games[room_id].game_state != 'waiting':
        emit('error', {'message': '遊戲已開始，無法加入'})
        return
    player_id = games[room_id].add_player(data['player_name'], request.sid)
    join_room(room_id)
    emit('joined_room', {
        'player_id': player_id,
        'game_state': games[room_id].get_game_state(player_id)
    })
    socketio.emit('player_joined', {
        'player_name': data['player_name'],
        'game_state': games[room_id].get_game_state()
    }, room=room_id)

@socketio.on('set_roles')
def handle_set_roles(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    if player_id != game.host_id:
        emit('error', {'message': '只有房主可以設置角色'})
        return
    game.set_custom_roles(data['roles'])
    socketio.emit('roles_updated', {
        'roles': data['roles'],
        'game_state': game.get_game_state()
    }, room=room_id)

@socketio.on('start_game')
def handle_start_game(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    if player_id != game.host_id:
        emit('error', {'message': '只有房主可以開始遊戲'})
        return
    success, message = game.start_game()
    if success:
        join_wolf_room(game, room_id)
        for pid, player in game.players.items():
            role_info = game.get_player_role_info(pid)
            socketio.emit('role_assigned', {
                'role_info': role_info,
                'game_state': game.get_game_state(pid)
            }, room=player['socket_id'])
    else:
        emit('error', {'message': message})

@socketio.on('night_action')
def handle_night_action(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    success, message = game.night_action(
        player_id, data['action_type'],
        data.get('target_id'), data.get('additional_target')
    )
    emit('action_result', {'success': success, 'message': message})

@socketio.on('night_confirm')
def handle_night_confirm(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    everyone_done = game.confirm_night(player_id)
    if everyone_done:
        success, results = game.process_night()
        if success:
            for result in results:
                if result['type'] == 'check':
                    socketio.emit('check_result', result, room=game.players[result['player_id']]['socket_id'])
            socketio.emit('phase_changed', {
                'new_phase': game.game_state,
                'game_state': game.get_game_state()
            }, room=room_id)
        game.night_actions = {}

@socketio.on('day_action')
def handle_day_action(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    success, message = game.day_action(
        player_id, data['action_type'], data.get('target_id')
    )
    emit('action_result', {'success': success, 'message': message})

@socketio.on('day_confirm')
def handle_day_confirm(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    everyone_done = game.confirm_day(player_id)
    if everyone_done:
        game.game_state = "voting"
        socketio.emit('phase_changed', {
            'new_phase': 'voting',
            'game_state': game.get_game_state()
        }, room=room_id)

@socketio.on('vote')
def handle_vote(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    success, message = game.vote(player_id, data['target_id'])
    emit('vote_result', {'success': success, 'message': message})

@socketio.on('vote_confirm')
def handle_vote_confirm(data):
    room_id = data['room_id']
    player_id = data['player_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    everyone_done = game.confirm_vote(player_id)
    if everyone_done:
        success, message = game.process_vote()
        if success:
            socketio.emit('phase_changed', {
                'new_phase': game.game_state,
                'game_state': game.get_game_state()
            }, room=room_id)
        game.votes = {}

@socketio.on('wolf_king_revenge')
def handle_wolf_king_revenge(data):
    room_id = data['room_id']
    target_id = data['target_id']
    if room_id not in games:
        emit('error', {'message': '房間不存在'})
        return
    game = games[room_id]
    if not game.revenge_waiting:
        emit('error', {'message': '沒有狼王需要報復'})
        return
    game.wolf_king_revenge(target_id)
    socketio.emit('phase_changed', {
        'new_phase': game.game_state,
        'game_state': game.get_game_state()
    }, room=room_id)

@socketio.on('wolf_night_chat')
def handle_wolf_night_chat(data):
    room_id = data['room_id']
    player_id = data['player_id']
    message = data['message']
    game = games.get(room_id)
    if not game:
        emit('error', {'message': '房間不存在'})
        return
    player = game.players.get(player_id)
    if not player or not player['alive'] or game.all_roles[player['role']]['team'] != 'werewolf':
        emit('error', {'message': '你不是狼人或你已經死亡'})
        return
    wolf_room = room_id + "_wolves"
    socketio.emit('wolf_night_message', {
        'player_name': player['name'],
        'message': message
    }, room=wolf_room)

@socketio.on('disconnect')
def handle_disconnect():
    for room_id, game in games.items():
        for player_id, player in game.players.items():
            if player['socket_id'] == request.sid:
                game.remove_player(player_id)
                leave_room(room_id)
                socketio.emit('player_left', {
                    'player_name': player['name'],
                    'game_state': game.get_game_state()
                }, room=room_id)
                break




    # HTML模板
HTML_TEMPLATE ='''
<!DOCTYPE html>
<html>
<head>
    <title>狼人殺遊戲</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.0/socket.io.js"></script>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background: #1a1a1a;
            color: #fff;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .card {
            background: #2d2d2d;
            border-radius: 10px;
            padding: 20px;
            margin: 10px 0;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .btn {
            background: #4CAF50;
            color: white;
            padding: 10px 15px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            margin: 5px;
        }
        .btn:hover {
            background: #45a049;
        }
        .btn-danger {
            background: #f44336;
        }
        .btn-danger:hover {
            background: #da190b;
        }
        .btn-warning {
            background: #ff9800;
        }
        .btn-warning:hover {
            background: #e68900;
        }
        input, select {
            padding: 8px;
            margin: 5px;
            border: 1px solid #555;
            border-radius: 4px;
            background: #333;
            color: #fff;
        }
        .role-config {
            display: flex;
            align-items: center;
            margin: 10px 0;
        }
        .role-config label {
            min-width: 100px;
        }
        .players-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 10px;
        }
        .player-card {
            background: #3d3d3d;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }
        .player-alive {
            border-left: 4px solid #4CAF50;
        }
        .player-dead {
            border-left: 4px solid #f44336;
            opacity: 0.6;
        }
        .game-log {
            height: 200px;
            overflow-y: auto;
            background: #1e1e1e;
            padding: 10px;
            border-radius: 5px;
            font-family: monospace;
        }
        .hidden {
            display: none;
        }
        .role-info {
            background: #4a4a4a;
            padding: 15px;
            border-radius: 8px;
            margin: 10px 0;
        }
        .werewolf {
            background: #8b0000;
        }
        .village {
            background: #228b22;
        }
        .action-buttons {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 10px 0;
        }
        .phase-indicator {
            font-size: 24px;
            font-weight: bold;
            text-align: center;
            padding: 20px;
        }
        .night {
            background: #191970;
        }
        .day {
            background: #ffa500;
            color: #000;
        }
        .voting {
            background: #dc143c;
        }
        .ended {
            background: #666;
        }
        /* 狼人聊天室樣式 */
        #wolf-chat {
            margin-top: 10px;
            background: #232338;
            border: 2px solid #8b0000;
        }
        #wolf-chat-messages {
            height: 100px;
            overflow-y: auto;
            background: #222;
            padding: 8px;
            margin-bottom: 8px;
            border-radius: 4px;
            font-size: 15px;
        }
        #wolf-chat-input {
            width: 70%;
        }
        #witch-night-info {
            color: #ff0;
            margin-bottom: 8px;
        }
        #not-wolf-leader-tip {
            color: #ffb;
            background: #444;
            padding: 6px;
            border-radius: 5px;
            margin: 10px 0;
            font-size: 15px;
            text-align: center;
        }
    </style>
</head>
<body>
<div class="container">
    <h1>🐺 狼人殺遊戲</h1>
    <!-- 登入界面 -->
    <div id="login-screen" class="card">
        <h2>加入遊戲</h2>
        <input type="text" id="player-name" placeholder="輸入你的名字" maxlength="20">
        <br>
        <button class="btn" onclick="createRoom()">創建房間</button>
        <input type="text" id="room-id" placeholder="房間ID" maxlength="8">
        <button class="btn" onclick="joinRoom()">加入房間</button>
    </div>
    <!-- 房間設置界面 -->
    <div id="room-setup" class="card hidden">
        <h2>房間設置</h2>
        <p>房間ID: <span id="current-room-id"></span></p>
        <div id="host-controls" class="hidden">
            <h3>角色配置</h3>
            <div id="role-config">
                <div class="role-config"><label>村民:</label><input type="number" id="villager-count" min="0" max="20" value="2"></div>
                <div class="role-config"><label>狼人:</label><input type="number" id="werewolf-count" min="1" max="10" value="2"></div>
                <div class="role-config"><label>預言家:</label><input type="number" id="seer-count" min="0" max="2" value="1"></div>
                <div class="role-config"><label>女巫:</label><input type="number" id="witch-count" min="0" max="2" value="1"></div>
                <div class="role-config"><label>獵人:</label><input type="number" id="hunter-count" min="0" max="2" value="1"></div>
                <div class="role-config"><label>守衛:</label><input type="number" id="guard-count" min="0" max="2" value="1"></div>
                <div class="role-config"><label>狼王:</label><input type="number" id="wolf_king-count" min="0" max="2" value="0"></div>
                <div class="role-config"><label>白狼王:</label><input type="number" id="white_wolf_king-count" min="0" max="1" value="0"></div>
                <div class="role-config"><label>騎士:</label><input type="number" id="knight-count" min="0" max="2" value="0"></div>
                <div class="role-config"><label>白痴:</label><input type="number" id="idiot-count" min="0" max="1" value="0"></div>
            </div>
            <p>總角色數: <span id="total-roles">8</span> | 當前玩家數: <span id="current-players">0</span></p>
            <button class="btn" onclick="updateRoles()">更新角色配置</button>
            <button class="btn btn-warning" onclick="startGame()">開始遊戲</button>
        </div>
    </div>
    <!-- 遊戲界面 -->
    <div id="game-screen" class="hidden">
        <div id="phase-indicator" class="phase-indicator">等待開始</div>
        <div id="role-info" class="role-info hidden">
            <h3>你的角色</h3>
            <p><strong>角色:</strong> <span id="my-role"></span></p>
            <p><strong>陣營:</strong> <span id="my-team"></span></p>
            <p><strong>能力:</strong> <span id="my-ability"></span></p>
            <div id="teammates" class="hidden">
                <p><strong>隊友:</strong> <span id="teammate-list"></span></p>
            </div>
        </div>
        <!-- 狼人聊天室 -->
        <div id="wolf-chat" class="card hidden">
            <h4>狼人聊天室（僅夜晚狼人可見）</h4>
            <div id="wolf-chat-messages"></div>
            <input id="wolf-chat-input" type="text" placeholder="輸入訊息">
            <button onclick="sendWolfMessage()">發送</button>
        </div>
        <div id="action-area" class="card">
            <h3>行動區域</h3>
            <div id="night-actions" class="hidden">
                <h4>夜晚行動</h4>
                <div id="not-wolf-leader-tip" class="hidden">狼人請在聊天室討論，由「首狼人」代表決定殺人對象。</div>
                <div id="witch-night-info" class="hidden"></div>
                <div class="action-buttons" id="night-buttons"></div>
                <select id="night-target" class="hidden">
                    <option value="">選擇目標</option>
                </select>
                <select id="additional-target" class="hidden">
                    <option value="">選擇第二個目標</option>
                </select>
                <button class="btn hidden" id="confirm-night-action" onclick="confirmNightAction()">確認行動</button>
                <button class="btn hidden" id="night-confirm-btn" onclick="nightConfirm()">我已完成夜晚行動</button>
            </div>
            <div id="day-actions" class="hidden">
                <h4>白天行動</h4>
                <div class="action-buttons" id="day-buttons"></div>
                <select id="day-target" class="hidden">
                    <option value="">選擇目標</option>
                </select>
                <button class="btn hidden" id="confirm-day-action" onclick="confirmDayAction()">確認行動</button>
                <button class="btn hidden" id="day-confirm-btn" onclick="dayConfirm()">我已完成白天行動</button>
            </div>
            <div id="voting-area" class="hidden">
                <h4>投票出局</h4>
                <select id="vote-target">
                    <option value="">選擇投票目標</option>
                </select>
                <button class="btn" onclick="vote()">投票</button>
                <button class="btn" id="vote-confirm-btn" onclick="voteConfirm()">我已完成投票</button>
            </div>
        </div>
        <div class="card">
            <h3>玩家列表</h3>
            <div id="players-list" class="players-grid"></div>
        </div>
        <div class="card">
            <h3>遊戲記錄</h3>
            <div id="game-log" class="game-log"></div>
        </div>
    </div>
</div>
<script>
const socket = io();
let currentRoomId = null;
let currentPlayerId = null;
let isHost = false;
let myRole = null;
let gameState = null;
let witchKilledId = null;

// 角色配置更新
function updateRoleCount() {
    const roles = ['villager', 'werewolf', 'seer', 'witch', 'hunter', 'guard', 'wolf_king', 'white_wolf_king', 'knight', 'idiot'];
    let total = 0;
    roles.forEach(role => {
        const count = parseInt(document.getElementById(role + '-count').value) || 0;
        total += count;
    });
    document.getElementById('total-roles').textContent = total;
}
document.addEventListener('DOMContentLoaded', function() {
    const roleInputs = document.querySelectorAll('#role-config input[type="number"]');
    roleInputs.forEach(input => {
        input.addEventListener('change', updateRoleCount);
    });
    updateRoleCount();
});
function createRoom() {
    const playerName = document.getElementById('player-name').value.trim();
    if (!playerName) { alert('請輸入玩家名字'); return; }
    socket.emit('create_room', { player_name: playerName });
}
function joinRoom() {
    const playerName = document.getElementById('player-name').value.trim();
    const roomId = document.getElementById('room-id').value.trim();
    if (!playerName || !roomId) { alert('請輸入玩家名字和房間ID'); return; }
    socket.emit('join_room', { player_name: playerName, room_id: roomId });
}
function updateRoles() {
    const roles = [];
    const roleNames = ['villager', 'werewolf', 'seer', 'witch', 'hunter', 'guard', 'wolf_king', 'white_wolf_king', 'knight', 'idiot'];
    roleNames.forEach(role => {
        const count = parseInt(document.getElementById(role + '-count').value) || 0;
        if (count > 0) { roles.push({ role: role, count: count }); }
    });
    socket.emit('set_roles', { room_id: currentRoomId, player_id: currentPlayerId, roles: roles });
}
function startGame() {
    socket.emit('start_game', { room_id: currentRoomId, player_id: currentPlayerId });
}
function confirmNightAction() {
    const actionType = document.querySelector('#night-buttons .btn.selected')?.dataset.action;
    const target = document.getElementById('night-target').value;
    const additionalTarget = document.getElementById('additional-target').value;
    if (!actionType) { alert('請選擇行動類型'); return; }
    if (!target && actionType !== 'peek') { alert('請選擇目標'); return; }
    socket.emit('night_action', {
        room_id: currentRoomId,
        player_id: currentPlayerId,
        action_type: actionType,
        target_id: target || null,
        additional_target: additionalTarget || null
    });
}
function nightConfirm() {
    socket.emit('night_confirm', { room_id: currentRoomId, player_id: currentPlayerId });
    document.getElementById('night-confirm-btn').classList.add('hidden');
}
function confirmDayAction() {
    const actionType = document.querySelector('#day-buttons .btn.selected')?.dataset.action;
    const target = document.getElementById('day-target').value;
    if (!actionType || !target) { alert('請選擇行動類型和目標'); return; }
    socket.emit('day_action', {
        room_id: currentRoomId,
        player_id: currentPlayerId,
        action_type: actionType,
        target_id: target
    });
}
function dayConfirm() {
    socket.emit('day_confirm', { room_id: currentRoomId, player_id: currentPlayerId });
    document.getElementById('day-confirm-btn').classList.add('hidden');
}
// 狼人聊天室
function sendWolfMessage() {
    const msg = document.getElementById('wolf-chat-input').value.trim();
    if (!msg) return;
    socket.emit('wolf_night_chat', {
        room_id: currentRoomId,
        player_id: currentPlayerId,
        message: msg
    });
    document.getElementById('wolf-chat-input').value = '';
}
socket.on('wolf_night_message', function(data) {
    const msgDiv = document.createElement('div');
    msgDiv.textContent = `${data.player_name}: ${data.message}`;
    document.getElementById('wolf-chat-messages').appendChild(msgDiv);
    const box = document.getElementById('wolf-chat-messages');
    box.scrollTop = box.scrollHeight;
});
// 狼王報復彈窗
function showWolfKingRevengeSelector(alivePlayers) {
    const selector = document.createElement('select');
    selector.innerHTML = '<option value="">選擇要帶走的玩家</option>';
    alivePlayers.forEach(player => {
        if (player.id !== currentPlayerId) {
            selector.innerHTML += `<option value="${player.id}">${player.name}</option>`;
        }
    });
    const btn = document.createElement('button');
    btn.textContent = '確定帶走';
    btn.className = 'btn btn-danger';
    btn.onclick = function() {
        const val = selector.value;
        if (!val) { alert('請選擇玩家'); return; }
        socket.emit('wolf_king_revenge', {
            room_id: currentRoomId,
            target_id: val
        });
        document.body.removeChild(document.getElementById('wolfking-revenge-modal'));
    };
    const modal = document.createElement('div');
    modal.id = 'wolfking-revenge-modal';
    modal.style = 'position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;z-index:10000;';
    modal.innerHTML = `<div style="background:#333;color:#fff;padding:40px;border-radius:12px;text-align:center;">
        <h2>你是狼王，請選擇要帶走的人</h2>
    </div>`;
    modal.querySelector('div').appendChild(selector);
    modal.querySelector('div').appendChild(document.createElement('br'));
    modal.querySelector('div').appendChild(btn);
    document.body.appendChild(modal);
}
function vote() {
    const target = document.getElementById('vote-target').value;
    if (!target) { alert('請選擇投票目標'); return; }
    socket.emit('vote', { room_id: currentRoomId, player_id: currentPlayerId, target_id: target });
}
function voteConfirm() {
    socket.emit('vote_confirm', { room_id: currentRoomId, player_id: currentPlayerId });
    document.getElementById('vote-confirm-btn').classList.add('hidden');
}
function updatePlayersList(players) {
    const playersContainer = document.getElementById('players-list');
    playersContainer.innerHTML = '';
    players.forEach(player => {
        const playerDiv = document.createElement('div');
        playerDiv.className = `player-card ${player.alive ? 'player-alive' : 'player-dead'}`;
        let roleInfo = '';
        if (player.role) {
            roleInfo = `<br><small>${player.role} (${player.team})</small>`;
        }
        playerDiv.innerHTML = `
            <strong>${player.name}</strong>
            ${roleInfo}
            <br><small>${player.alive ? '存活' : '死亡'}</small>
            ${!player.can_vote && player.alive ? '<br><small>無投票權</small>' : ''}
        `;
        playersContainer.appendChild(playerDiv);
    });
    updateTargetSelectors(players);
}
function updateTargetSelectors(players) {
    const alivePlayers = players.filter(p => p.alive && p.id !== currentPlayerId);
    const nightTarget = document.getElementById('night-target');
    nightTarget.innerHTML = '<option value="">選擇目標</option>';
    alivePlayers.forEach(player => {
        nightTarget.innerHTML += `<option value="${player.id}">${player.name}</option>`;
    });
    const additionalTarget = document.getElementById('additional-target');
    additionalTarget.innerHTML = '<option value="">選擇第二個目標</option>';
    players.filter(p => p.alive).forEach(player => {
        additionalTarget.innerHTML += `<option value="${player.id}">${player.name}</option>`;
    });
    const dayTarget = document.getElementById('day-target');
    dayTarget.innerHTML = '<option value="">選擇目標</option>';
    alivePlayers.forEach(player => {
        dayTarget.innerHTML += `<option value="${player.id}">${player.name}</option>`;
    });
    const voteTarget = document.getElementById('vote-target');
    voteTarget.innerHTML = '<option value="">選擇投票目標</option>';
    alivePlayers.forEach(player => {
        voteTarget.innerHTML += `<option value="${player.id}">${player.name}</option>`;
    });
}
function updateActionButtons(roleInfo, gameState) {
    const nightButtons = document.getElementById('night-buttons');
    const dayButtons = document.getElementById('day-buttons');
    nightButtons.innerHTML = '';
    dayButtons.innerHTML = '';
    document.getElementById('not-wolf-leader-tip').classList.add('hidden');
    document.getElementById('confirm-night-action').classList.add('hidden');
    // 狼人代表制
    if (!roleInfo) return;
    if (gameState === 'night') {
        if (roleInfo.ability === 'kill') {
            if (roleInfo.wolf_leader) {
                addActionButton(nightButtons, 'kill', '殺人');
                document.getElementById('confirm-night-action').classList.remove('hidden');
            } else {
                // 不是首狼不能殺人，顯示提示
                document.getElementById('not-wolf-leader-tip').classList.remove('hidden');
            }
        }
        // 其他夜晚角色
        if (roleInfo.ability && roleInfo.ability !== 'kill') {
            if (roleInfo.ability === 'check') addActionButton(nightButtons, 'check', '查驗');
            if (roleInfo.ability === 'protect') addActionButton(nightButtons, 'protect', '守護');
            if (roleInfo.ability === 'potion') {
                if (roleInfo.potions && roleInfo.potions.poison) addActionButton(nightButtons, 'poison', '毒殺');
                if (roleInfo.potions && roleInfo.potions.antidote) addActionButton(nightButtons, 'antidote', '解藥');
            }
            if (roleInfo.ability === 'exchange') addActionButton(nightButtons, 'exchange', '交換');
            if (roleInfo.ability === 'peek') addActionButton(nightButtons, 'peek', '偷看');
            document.getElementById('confirm-night-action').classList.remove('hidden');
        }
    }
    if (gameState === 'day') {
        if (roleInfo.ability === 'duel') addActionButton(dayButtons, 'duel', '決鬥');
        if (roleInfo.ability === 'self_destruct') addActionButton(dayButtons, 'self_destruct', '自爆');
        document.getElementById('confirm-day-action').classList.remove('hidden');
    }
}
function addActionButton(container, action, text) {
    const button = document.createElement('button');
    button.className = 'btn';
    button.textContent = text;
    button.dataset.action = action;
    button.onclick = function() {
        container.querySelectorAll('.btn').forEach(btn => btn.classList.remove('selected'));
        this.classList.add('selected');
        if (action === 'exchange') {
            document.getElementById('night-target').classList.remove('hidden');
            document.getElementById('additional-target').classList.remove('hidden');
        } else if (action !== 'peek') {
            if (container.id === 'night-buttons') {
                document.getElementById('night-target').classList.remove('hidden');
            } else {
                document.getElementById('day-target').classList.remove('hidden');
            }
        }
        if (container.id === 'night-buttons') {
            document.getElementById('confirm-night-action').classList.remove('hidden');
        } else {
            document.getElementById('confirm-day-action').classList.remove('hidden');
        }
    };
    container.appendChild(button);
}
function updateGameState(state) {
    gameState = state;
    document.getElementById('current-players').textContent = state.players.length;
    const phaseIndicator = document.getElementById('phase-indicator');
    const phaseText = {
        'waiting': '等待開始',
        'night': `第${state.day_count}夜`,
        'day': `第${state.day_count}天`,
        'voting': '投票階段',
        'ended': '遊戲結束',
        'wolf_king_revenge': '狼王報復'
    };
    phaseIndicator.textContent = phaseText[state.game_state] || state.game_state;
    phaseIndicator.className = `phase-indicator ${state.game_state}`;
    updatePlayersList(state.players);
    const gameLog = document.getElementById('game-log');
    gameLog.innerHTML = state.game_log.map(log => `<div>${log}</div>`).join('');
    gameLog.scrollTop = gameLog.scrollHeight;
    document.getElementById('night-actions').classList.toggle('hidden', state.game_state !== 'night');
    document.getElementById('day-actions').classList.toggle('hidden', state.game_state !== 'day');
    document.getElementById('voting-area').classList.toggle('hidden', state.game_state !== 'voting');
    document.getElementById('host-game-controls').classList.add('hidden'); // 全員確定，不再顯示房主按鈕
    isHost = state.is_host;
    // 狼人聊天室只在夜晚且自己是狼人可見
    if (state.game_state === 'night' && myRole && myRole.team === 'werewolf') {
        document.getElementById('wolf-chat').classList.remove('hidden');
    } else {
        document.getElementById('wolf-chat').classList.add('hidden');
        document.getElementById('wolf-chat-messages').innerHTML = '';
    }
    // 狼王報復觸發
    if (state.game_state === "wolf_king_revenge" && state.revenge_waiting && state.revenge_waiting.wolf_king_id === currentPlayerId) {
        showWolfKingRevengeSelector(state.players.filter(p => p.alive && p.id !== currentPlayerId));
    } else if (document.getElementById('wolfking-revenge-modal')) {
        document.body.removeChild(document.getElementById('wolfking-revenge-modal'));
    }
    // 確認按鈕顯示判斷
    if (state.game_state === 'night') {
        document.getElementById('night-confirm-btn').classList.remove('hidden');
    } else {
        document.getElementById('night-confirm-btn').classList.add('hidden');
    }
    if (state.game_state === 'day') {
        document.getElementById('day-confirm-btn').classList.remove('hidden');
    } else {
        document.getElementById('day-confirm-btn').classList.add('hidden');
    }
    if (state.game_state === 'voting') {
        document.getElementById('vote-confirm-btn').classList.remove('hidden');
    } else {
        document.getElementById('vote-confirm-btn').classList.add('hidden');
    }
    // 女巫夜晚資訊顯示重設
    document.getElementById('witch-night-info').classList.add('hidden');
    document.getElementById('witch-night-info').textContent = '';
    // 狼人首領提示重設
    document.getElementById('not-wolf-leader-tip').classList.add('hidden');
}
// 女巫夜晚得知誰被殺
socket.on('witch_night_info', function(data) {
    if (data && data.killed_player_id && data.killed_player_name) {
        document.getElementById('witch-night-info').classList.remove('hidden');
        document.getElementById('witch-night-info').textContent = `今晚被殺的是：${data.killed_player_name}`;
        witchKilledId = data.killed_player_id;
    } else {
        document.getElementById('witch-night-info').classList.add('hidden');
        document.getElementById('witch-night-info').textContent = '';
        witchKilledId = null;
    }
});
socket.on('room_created', function(data) {
    currentRoomId = data.room_id;
    currentPlayerId = data.player_id;
    document.getElementById('current-room-id').textContent = currentRoomId;
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('room-setup').classList.remove('hidden');
    document.getElementById('host-controls').classList.remove('hidden');
    updateGameState(data.game_state);
});
socket.on('joined_room', function(data) {
    currentPlayerId = data.player_id;
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('room-setup').classList.remove('hidden');
    updateGameState(data.game_state);
});
socket.on('player_joined', function(data) { updateGameState(data.game_state); });
socket.on('roles_updated', function(data) { updateGameState(data.game_state); });
socket.on('role_assigned', function(data) {
    myRole = data.role_info;
    document.getElementById('my-role').textContent = myRole.role;
    document.getElementById('my-team').textContent = myRole.team === 'werewolf' ? '狼人陣營' : '好人陣營';
    document.getElementById('my-ability').textContent = myRole.description;
    if (myRole.teammates && myRole.teammates.length > 0) {
        document.getElementById('teammates').classList.remove('hidden');
        document.getElementById('teammate-list').textContent = myRole.teammates.map(t => t.name).join(', ');
    } else {
        document.getElementById('teammates').classList.add('hidden');
    }
    document.getElementById('room-setup').classList.add('hidden');
    document.getElementById('game-screen').classList.remove('hidden');
    document.getElementById('role-info').classList.remove('hidden');
    updateGameState(data.game_state);
    updateActionButtons(myRole, data.game_state.game_state);
});
socket.on('phase_changed', function(data) {
    updateGameState(data.game_state);
    if (myRole) {
        updateActionButtons(myRole, data.game_state.game_state);
    }
});
socket.on('check_result', function(data) {
    alert(`查驗結果：${data.target_name} 是 ${data.result}`);
});
socket.on('day_action_result', function(data) {
    alert(data.message);
    updateGameState(data.game_state);
});
socket.on('action_result', function(data) {
    if (data.success) {
        alert('行動成功：' + data.message);
        document.querySelectorAll('#night-buttons .btn, #day-buttons .btn').forEach(btn => btn.classList.remove('selected'));
        document.getElementById('night-target').classList.add('hidden');
        document.getElementById('additional-target').classList.add('hidden');
        document.getElementById('day-target').classList.add('hidden');
        document.getElementById('confirm-night-action').classList.add('hidden');
        document.getElementById('confirm-day-action').classList.add('hidden');
    } else {
        alert('行動失敗：' + data.message);
    }
});
socket.on('vote_result', function(data) {
    if (data.success) { alert('投票成功'); }
    else { alert('投票失敗：' + data.message); }
});
socket.on('error', function(data) {
    alert('錯誤：' + data.message);
});
const style = document.createElement('style');
style.textContent = `.btn.selected { background: #ff6b6b !important; transform: scale(1.05); }`;
document.head.appendChild(style);
</script>
</body>
</html>
'''
if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
