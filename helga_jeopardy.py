import datetime
import nltk
import random
import re
import requests
import smokesignal
import string

from helga import settings, log
from helga.db import db
from helga.plugins import command

from bson.son import SON
from difflib import SequenceMatcher
from nltk.corpus import stopwords
from nltk.stem.snowball import EnglishStemmer
from requests.exceptions import RequestException
from twisted.internet import reactor

logger = log.getLogger(__name__)

DEBUG = getattr(settings, 'HELGA_DEBUG', False)
ANSWER_DELAY = getattr(settings, 'JEOPARDY_ANSWER_DELAY', 30)
GAME_ANSWER_DELAY = getattr(settings, 'JEOPARDY_MATCH_ANSWER_DELAY', 15)
CHANNEL_ANNOUNCEMENT = getattr(settings, 'JEOPARDY_JOIN_MESSAGE', '')

URL_RE = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')

api_endpoint = 'http://jservice.io/api/'

correct_responses = [
    'look at the big brains on {}',
    '{}, you are correct.',
    '{} takes it, and has control of the board.',
]

game_correct_responses_maintain = [
    'Well done, {}. Now select another clue from our remaining categories.',
    '{}, correct! You are still in control of the board.',
    '{} takes it, and maintains control of the board.',
    'That\'s right, {}. You still have control, pick again.',
    '{}, yes. Select again.',
]

game_correct_responses_take = [
    'Well done, {}. You now have control of the board.',
    '{}, correct! You have taken control of the board.',
    '{} takes it, and takes control of the board.',
    'That\'s it! {} now has control of the board.',
    'You got it! You\'ve gained control of the board, {}.',
]

def reset_channel(channel, mongo_db=db.jeopardy):
    """
    For channel name, make sure no question is active.
    """

    logger.debug('resetting channel')

    mongo_db.update_many({
        'channel': channel,
        'active': True
    }, {'$set': {
        'active': False
    }})

    mongo_db.update_many({
        'channel': channel,
        'game_active': True
    }, {'$set': {
        'game_active': False,
        'game_started': False
    }})    

remove_punctuation_map = dict((ord(char), None) for char in string.punctuation)

def process_token(token):
    """
    stuff we do to every token, both answer and responses.

    1. cast to unicode and lower case
    2. remove punctuation
    3. stem

    """

    # cast to unicode and lower case
    token = u'{}'.format(token).lower()

    # remove punctuation
    token = token.translate(remove_punctuation_map)

    # stem
    stemmer = EnglishStemmer()
    token = stemmer.stem(token)

    return token

def eval_potential_answer(input_line, answer):
    """
    Checks if `input_line` is an match for `answer`

    returns a 3 item tuple:
    `bool`: True if correct
    `partial`: number of tokens matched
    `ratio`: ratio of matching characters

    """

    pot_answers = re.findall(r'\([^()]*\)|[^()]+', answer)

    if len(pot_answers) == 2:
        for pot_answer in pot_answers:
            pot_answer = pot_answer.replace('(','').replace(')','')
            correct, _, _ = eval_potential_answer(input_line, pot_answer)

            if correct:
                return correct, None, None

    correct = False
    partial = 0
    ratio = 0.0

    input_string = u''.join(input_line)

    sequence_matcher = SequenceMatcher(None, input_string, answer)
    ratio = sequence_matcher.ratio()

    if ratio >= 0.75:
        correct = True

    input_tokens = [process_token(token) for token in input_line]
    processed_answer_tokens = [process_token(token) for token in answer.split()]
    answer_tokens = []

    for tok in processed_answer_tokens:
        if tok not in stopwords.words('english'):
            answer_tokens.append(tok)

    # remove stopwords from answer_tokens

    matched = set(input_tokens).intersection(set(answer_tokens))
    partial = len(matched)

    logger.debug(u'matched: {}'.format(matched))
    logger.debug(u'ratio: {}'.format(ratio))

    if len(matched) == len(answer_tokens):
        correct = True

    return correct, partial, ratio

def reveal_answer(client, channel, question_id, answer, mongo_db=db.jeopardy, random=True):
    """
    This is the timer, essentially. When this point is reached, no more
    answers will be accepted, and our gracious host will reveal the
    answer in all of it's glory.
    """

    logger.debug('time to reveal the answer, if no one has guess')

    question = mongo_db.find_one({
        '_id': question_id,
    })

    if not question:
        logger.warning('no question found, not good')
        return

    if not question['active']:
        logger.debug('not active question, someone must have answered it! Good Show!')
        return

    if random:
        client.msg(channel, u'the correct answer is: {}'.format(answer))
        mongo_db.update({
            '_id': question_id,
        }, {
            '$set': {
                'active': False,
            }
        })

    else:
        client.msg(channel, u'the correct answer is: {}'.format(answer))
        clue_active = question['category'] + '.' + question['clue_idx'] + '.active'
        mongo_db.update({
            'game_active': True,
            'channel': channel,
        }, {
            '$set': {
                clue_active: False,
            }
        })

        mongo_db.update({
            '_id': question_id,
        }, {
            '$set': {
                'active': False,
            }
        })
        if check_remaining_clues(client, channel) is False:
            return

        show_board(client, channel)

def retrieve_question(client, channel, current_game=None, sel_category=None, sel_value=None, random=True):
    """
    Return the question and correct answer.

    Adds question to the database, which is how it is tracked until
    active=False.

    """

    if random is True:
        logger.debug('initiating question retrieval')
    
        try:
            api_resp = requests.get('{}random.json'.format(api_endpoint))
        except RequestException as e:
            logger.warn("Error fetching question from jservice API: %d %s", e.response.status_code, e.response.reason)
            return "Could not retrieve a question from the jservice API"
    
        json_resp = tb_resp.json()[0]
        question_text = json_resp['question']
        answer = json_resp['answer']
        category = json_resp['category']['title']
        value = json_resp['value']
    
        if DEBUG:
            logger.debug(u'psst! the answer is: {}'.format(answer))
    
        question_id = db.jeopardy.insert({
            'question': question_text,
            'answer': answer,
            'channel': channel,
            'value': value,
            'active': True,
        })
    
        question = u'[{}] For ${}: {}'.format(category, value, question_text)
    
        logger.debug(u'will reveal answer in {} seconds'.format(ANSWER_DELAY))
    
        reactor.callLater(ANSWER_DELAY, reveal_answer, client, channel, question_id, answer)
    
        return question

    else:
        logger.debug('initiating question retrieval')
        q_active = 'not_found'
        for key, value in current_game.items():
            if key == sel_category:
                for k, v in value.items():
                    if k.startswith('clue') and v['value'] == sel_value:
                        question = v['question']
                        answer_raw = v['answer']
                        q_active = v['active']
                        clue = k

        category = current_game[sel_category]['category']
        if q_active is False:
            client.msg(channel, "Clue has already been played. Choose another clue")
            return
        
        if q_active == 'not_found':
            client.msg(channel, "Clue not found, select again.")
            return

        question_text = str(question).strip('[]\'')
        answer = str(answer_raw).strip('[]\'')
        if DEBUG:
            logger.debug(u'psst! the answer is: {}'.format(answer))

        question_id = db.jeopardy.insert({
            'question': question_text,
            'answer': answer,
            'channel': channel,
            'value': sel_value,
            'category': sel_category,
            'clue_idx': clue,
            'active': True,
        })

        question = u'[{}] For ${}: {}'.format(category, sel_value, question_text)

        logger.debug(u'will reveal answer in {} seconds'.format(GAME_ANSWER_DELAY))

        reactor.callLater(GAME_ANSWER_DELAY, reveal_answer, client, channel, question_id, answer, random=False)

        return question


def clean_question(question):
    """
    Cleans question text.
    :param question: The raw question text.
    :return: A 2-tuple of the shape (<Resulting question>, <List of contextual messages to send before the question>)
    """
    contexts = []
    result = question

    url_matches = re.findall(URL_RE, question)
    if any(url_matches):
        result = re.sub(URL_RE, "", question)
        contexts += url_matches

    return result.strip(), contexts


def scores(client, channel, nick, alltime=False):
    """
    Returns top 3 scores in past week, plus the score of requesting
    nick, if the requesting nick is not in the top 3.
    """

    max_number = 3

    if alltime:
        max_number = 5

    pipeline = [
        {'$match': {
            'channel': channel,
        }},
        { '$group': {'_id': '$answered_by', 'money': {'$sum': '$value' }}},
        { '$sort': SON([('money', -1), ('_id', -1)])}
    ]

    title = "Jeopardy Leaderboard"

    if not alltime:
        title += " (Past 7 Days)"
        start_date = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        pipeline[0]['$match']['timestamp'] = {'$gte': start_date }
    else:
        title += " Hall of Game"

    leaderboard = [leader_obj for leader_obj in db.jeopardy.aggregate(pipeline)]
    rank = 1

    if len(leaderboard):
        client.msg(channel, title)

    for leader in leaderboard:

        if leader['_id'] is None:
            continue

        money = leader['money']
        money = (u'${:%d,.0f}'%(len(str(money))+1)).format(abs(money)).lstrip()

        if rank < max_number + 1:
            client.msg(channel, u"{}. {} -- {}".format(rank, leader['_id'], money))

        if leader['_id'] == nick:
            if rank >= max_number + 1:
                # i see you getting all judgey
                client.msg(channel, u"{}. {} -- {}".format(rank, leader['_id'], money))

        rank += 1

def fetch_categories():
    random_cat = random.randint(1,18412)
    cats_resp = requests.get('{}categories?count=6&offset={}'.format(api_endpoint, random_cat))
    y=0
    cat_dict={}
    for x in ["cat1", "cat2", "cat3", "cat4", "cat5", "cat6"]:
        cat_dict[x] = requests.get('{}category?id={}'.format(api_endpoint,cats_resp.json()[y]["id"]))
        y += 1
    return cat_dict

def setup_new_game(client, channel, nick, message, cmd, args, mongo_db=db.jeopardy):
    cat_dict = fetch_categories()
    print cat_dict

    for key, value in cat_dict.items():
        category = cat_dict[key].json()
        if len(category['clues']) < 5 or any(['question' not in q or not q['question'] for q in category['clues']]):
            client.msg(channel, "Category response was malformed, requesting again.")
            cat_dict = reactor.callLater(1, setup_new_game, client, channel, nick, message, cmd, args)
            return

    game = {
    'channel': channel,
    'game_active': True,
    'game_started': False,
    'game_host': nick,
    }
    for category_num in range(1, 7):
        category_name = 'cat{}'.format(category_num)
        category = cat_dict[category_name].json()
        game[category_name] = {'category': category['title']}
        set_value = 200
        for i in range(5):
            clue_name = 'clue{}'.format(i + 1)
            question = category['clues'][i]
            game_question = {
                'question': question['question'],
                'answer': question['answer'],
                'value': set_value,
                'id': question['id'],
                'active': True,
            }
            game[category_name][clue_name] = game_question
            set_value += 200

    game_id = db.jeopardy.insert(game)
    client.msg(channel, "New game created. to join: !j game join")
    return

def start_new_game(nick, new_game, client, channel, mongo_db=db.jeopardy):
    if nick not in new_game['players']:
        client.msg(channel, 'You have not joined the game lobby. Try: !j game join')
        return

    for player in new_game['players']:
        mongo_db.update({
            'game_active': True,
            'game_started': False,
        }, {
            '$set': {
                player: 0,
           }
        })
    client.msg(channel, 'Game started with the following players: {}'.format(', '.join(sorted(current_players))))
    client.msg(channel, "Here are today's categories:")
    show_board(client, channel, mongo_db=db.jeopardy)
    control = random.choice(new_game['players'])
    client.msg(channel, "By random choice, {} has control of the board".format(control))
    client.msg(channel, "To select a question, use the following format: !j cat3 200")
    mongo_db.update({
        'game_active': True,
        'game_started': False,
    }, {
        '$set': {
            'game_started': True,
            'control': control,
       }
    })
    reactor.callLater(1800, end_game, client, channel)
    return

def show_board(client, channel, mongo_db=db.jeopardy):
    current_game = mongo_db.find_one({
        'channel': channel,
        'game_active': True,
    })

    for key, value in current_game.items():
        if key.startswith('cat'):
            values = [str(value['clue{}'.format(i)]['value']) for i in range(1, 6) if value['clue{}'.format(i)]['active']]
            cat_names = value["category"]
            score_response =' '.join([key,  cat_names, ' '.join(sorted(values, key=int))])
            client.msg(channel, score_response)

    players = current_game['players']
    client.msg(channel, "Here are the current scores:")
    for i in players:
        client.msg(channel, "{} : {}".format(i, current_game[i]))

def evaluate_control(client, channel, nick, current_game, sel_category, sel_value, quest_func=retrieve_question):
    if current_game["control"] == nick:
        result = quest_func(client, channel, current_game, sel_category, sel_value, random=False)
        return result
    else:
        client.msg(channel, "You do not have control of the board")
        return

def check_remaining_clues(client, channel, mongo_db=db.jeopardy):
    current_game = mongo_db.find_one({
        'channel': channel,
        'game_active': True,
        'game_started': True,
    })
    question_count = 0
    for key, value in current_game.items():
        if key.startswith('cat'):
            for k, v in value.items():
                if k.startswith('clue'):
                    if v['active'] == True:
                        question_count += 1

    if question_count < 1:
        client.msg(channel, "All questions have been answered. Ending game..")
        end_game(client, channel, current_game)
        return False
    else:
        return True

def end_game(client, channel, current_game=None, mongo_db=db.jeopardy):
    mongo_db.update({
	'game_active': True,
	'channel': channel,
    }, {
	'$set': {
	    'game_active': False,
	    'game_started': False
	}
    })
    if current_game:
        current_players = current_game['players']
        client.msg(channel, "Here are the final scores:")
        scores = []
        for i in current_players:
	    client.msg(channel, "{} : {}".format(i, current_game[i]))
            scores.append(current_game[i])
        
        winners = []
        for key, value in current_game.items():
            if value == max(scores):
                winners.append(key)
    
        if len(winners) == 1:
            client.msg(channel, "{} is our new champion with a score of {}!".format(" ".join(winners), max(scores)))
            return
        else:
            client.msg(channel, "We have a tie! Our winners today are: {}  each of them scored {}".format(" ".join(winners), max(scores)))
            return
    else:
        client.msg(channel, "Game ended by host")
        return

@command('j', help='usage: ,j [<response>|score]')
def jeopardy(client, channel, nick, message, cmd, args,
             quest_func=retrieve_question, mongo_db=db.jeopardy):
    """
    Asks a question if there is no active question in the channel.

    If there are args and there is an active question, then evaluate
    the string as a possible answer.

    If there is an arg and there is no active question, ignore, was
    probably a late response.

    On the first correct response, deactivate the question and report
    the correct response (w/ nick).

    if the command 'score' is given, prints simple leaderboard

    """

    if args and args[0] == 'score':
        alltime = False
        if len(args) > 1 and args[1] == 'all':
            alltime = True

        return scores(client, channel, nick, alltime=alltime)

    if len(args) == 1 and args[0] == 'reset':
        reset_channel(channel, mongo_db)
        return 'done'


    # if we have an active question, and args, evaluate the answer

    question = mongo_db.find_one({
        'channel': channel,
        'active': True,
    })

    new_game = mongo_db.find_one({
        'channel': channel,
        'game_active': True,
        'game_started': False,
    })

    current_game = mongo_db.find_one({
        'channel': channel,
        'game_active': True,
        'game_started': True,
    })   

    if len(args) > 0 and args[0] == 'game':
        if args[1] == 'new':
            if new_game or current_game:
                client.msg(channel, "Game already created or in progress, the host can end the current game with: !j game end")
                return
            else:
                client.msg(channel, "Fetching questions and categories from the API. This will take ~10s")
                reactor.callLater(1,setup_new_game, client, channel, nick, message, cmd, args)
            return

    if len(args) == 2 and args[0] == 'game' and args[1] == 'end':
        if not new_game and not current_game:
            client.msg(channel, "No active game, try: !j game new")
            return
        if current_game:
            if nick == current_game['game_host']:
                client.msg(channel, "Host is ending game...")
                end_game(client, channel, current_game)
                return
            else:
                client.msg(channel, "Only the game host, {}, can end the game. As a failsafe, this game will end 30 minutes after start.".format(current_game['game_host']))
        if new_game and not current_game:
            if nick == new_game['game_host']:
                client.msg(channel, "Host is ending game...")
                end_game(client, channel)
                return
            else:
                client.msg(channel, "Only the game host, {}, can end the game. As a failsafe, this game will end 30 minutes after start.".format(current_game['game_host']))
        return

    if len(args) == 2 and args[0] == 'game' and args[1] == 'join':
        if not new_game:
            client.msg(channel, "No active game, try: !j game new")
            return

        current_players = []
        try:
            if new_game["players"]:
                for i in new_game["players"]:
                    current_players.append(i)

        except KeyError:
            pass

        for player in current_players:
            if nick == player:
                client.msg(channel, "You've already joined, {}".format(nick))
                return
        else:
            current_players.append(nick)

        mongo_db.update({
            'game_active': True,
            'game_started': False,
        }, {
            '$set': {
                'players': current_players[::],
           }
        })
        new_game = mongo_db.find_one({
            'channel': channel,
            'game_active': True,
            'game_started': False,
        })
        client.msg(channel, str(new_game["players"][::]))
        return

    if len(args) == 2 and args[0] == 'game' and args[1] == 'start':
        if not new_game:
            client.msg(channel, "No active game, try: !j game new")
            return

        if current_game:
            client.msg(channel, "Game already in progress")
            return

        if new_game["players"]:
            start_new_game(nick, new_game, client, channel)
            return
        else:
            client.msg(channel, "You need at least 1 player to start the game. try: !j game join")
            return

    if question and args:

        logger.debug('found active question')

        correct, partial, ratio = eval_potential_answer(args, question['answer'])

        if correct:

            logger.debug('answer is correct!')

            if current_game:
                if nick not in current_game['players']:
                    client.msg(channel, "You did not join this game. Please wait for the next game to join")
                    return
                sel_category = str(question['category']).strip('[]\'')
                clue_idx = str(question['clue_idx']).strip('[]\'')
                mongo_db.update({
                    'active': True,
                    'channel': channel,
                }, {
                    '$set': {
                        'active': False,
                    }
                })

                last_control = current_game['control']
                score = current_game[nick] 
                new_score = score + question['value']

                clue_active = sel_category + '.' + clue_idx + '.active'
                mongo_db.update({
                    'game_active': True,
                    'channel': channel,
                }, {
                    '$set': {
                        'control': nick,
                        clue_active: False,
                        nick: new_score,
                    }
                })

                if check_remaining_clues(client, channel) is False:
                    return

                reactor.callLater(1, show_board, client, channel)

                if last_control == nick:
                    return random.choice(game_correct_responses_maintain).format(nick)

                else:
                    return random.choice(game_correct_responses_take).format(nick)


            else:
                mongo_db.update({
                    'active': True,
                    'channel': channel,
                }, {
                    '$set': {
                        'active': False,
                        'answered_by': nick,
                        'timestamp': datetime.datetime.utcnow(),
                    }
                })

                return random.choice(correct_responses).format(nick)

        if partial > 0:
            return u"{}, can you be more specific?".format(nick)

        # wrong answer, ignore for random q, but deduct if game

        elif current_game:
                if nick not in current_game['players']:
                    client.msg(channel, "You did not join this game. Please wait for the next game to join")
                    return
                score = current_game[nick]
                new_score = score - question['value']
                mongo_db.update({
                    'game_active': True,
                    'channel': channel,
                }, {
                    '$set': {
                        nick: new_score,
                    }
                })
                return
            
        return

    if question and not args:
        logger.debug('no answer provided :/')
        return

    if not question and args:
        if args[0] != "game" and not args[0].startswith('cat'):
            logger.debug('no active question :/')
            return

    if args[0].startswith('cat'):
        if len(args) != 2:
            client.msg(channel, "You must provide a category and a value")
            return
        if not current_game:
            client.msg(channel, 'Game not started. To join: !j game join; to start: !j game start')
            return

        if nick not in current_game['players']:
            client.msg(channel, "Game in progress. You must wait for the next game to join")
            return

            

        sel_category = args[0]
        try:
            sel_value = int(args[1])
        except ValueError:
            client.msg(channel, "Value must be a number")
            return
        if args[0] not in ["cat1", "cat2", "cat3", "cat4", "cat5", "cat6"]:
            client.msg(channel, "Invalid category, select again.")
            return
        if current_game:
            question_text = evaluate_control(client, channel, nick, current_game, sel_category, sel_value)
            if not question_text:
                return
    if not current_game:
        question_text = quest_func(client, channel)

    result, context_messages = clean_question(question_text)
    for m in context_messages:
        client.msg(channel, m)

    return result


@smokesignal.on('join')
def back_from_commercial(client, channel):
    logger.info('Joined %s, resetting jeopardy state', channel)

    if CHANNEL_ANNOUNCEMENT:
        client.msg(channel, CHANNEL_ANNOUNCEMENT)

    reset_channel(channel)

    nltk.download('stopwords')
