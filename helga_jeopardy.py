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
CHANNEL_ANNOUNCEMENT = getattr(settings, 'JEOPARDY_JOIN_MESSAGE', '')

URL_RE = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')

api_endpoint = 'http://jservice.io/api/'

correct_responses = [
    'look at the big brains on {}',
    '{}, you are correct.',
    '{} takes it, and has control of the board.',
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

def reveal_answer(client, channel, question_id, answer, mongo_db=db.jeopardy):
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

    client.msg(channel, u'the correct answer is: {}'.format(answer))

    mongo_db.update({
        '_id': question_id,
    }, {
        '$set': {
            'active': False,
        }
    })

def retrieve_question(client, channel):
    """
    Return the question and correct answer.

    Adds question to the database, which is how it is tracked until
    active=False.

    """

    logger.debug('initiating question retrieval')

    try:
        tb_resp = requests.get('{}random.json'.format(api_endpoint))
    except RequestException:
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

def setup_new_game(client, channel, nick, message, cmd, args, mongo_db=db.jeopardy):

    cats_resp = requests.get('{}categories?count=6'.format(api_endpoint))
    y=0
    cat_dict={}
    for x in ["cat1", "cat2", "cat3", "cat4", "cat5", "cat6"]:
        cat_dict[x] = requests.get('{}category?id={}'.format(api_endpoint,cats_resp.json()[y]["id"]))
        y += 1

    c1title = cat_dict["cat1"].json()['title']
    c1q1question = cat_dict["cat1"].json()["clues"][0]["question"]
    c1q1answer = cat_dict["cat1"].json()["clues"][0]["answer"]
    c1q1id = cat_dict["cat1"].json()["clues"][0]["id"]
    c1q1value = cat_dict["cat1"].json()["clues"][0]["value"]

    c1q2question = cat_dict["cat1"].json()["clues"][1]["question"]
    c1q2answer = cat_dict["cat1"].json()["clues"][1]["answer"]
    c1q2id = cat_dict["cat1"].json()["clues"][1]["id"]
    c1q2value = cat_dict["cat1"].json()["clues"][1]["value"]

    c1q3question = cat_dict["cat1"].json()["clues"][2]["question"]
    c1q3answer = cat_dict["cat1"].json()["clues"][2]["answer"]
    c1q3id = cat_dict["cat1"].json()["clues"][2]["id"]
    c1q3value = cat_dict["cat1"].json()["clues"][2]["value"]

    c1q4question = cat_dict["cat1"].json()["clues"][3]["question"]
    c1q4answer = cat_dict["cat1"].json()["clues"][3]["answer"]
    c1q4id = cat_dict["cat1"].json()["clues"][3]["id"]
    c1q4value = cat_dict["cat1"].json()["clues"][3]["value"]

    c1q5question = cat_dict["cat1"].json()["clues"][4]["question"]
    c1q5answer = cat_dict["cat1"].json()["clues"][4]["answer"]
    c1q5id = cat_dict["cat1"].json()["clues"][4]["id"]
    c1q5value = cat_dict["cat1"].json()["clues"][4]["value"]


    c2title = cat_dict["cat2"].json()['title']
    c2q1question = cat_dict["cat2"].json()["clues"][0]["question"]
    c2q1answer = cat_dict["cat2"].json()["clues"][0]["answer"]
    c2q1id = cat_dict["cat2"].json()["clues"][0]["id"]
    c2q1value = cat_dict["cat2"].json()["clues"][0]["value"]

    c2q2question = cat_dict["cat2"].json()["clues"][1]["question"]
    c2q2answer = cat_dict["cat2"].json()["clues"][1]["answer"]
    c2q2id = cat_dict["cat2"].json()["clues"][1]["id"]
    c2q2value = cat_dict["cat2"].json()["clues"][1]["value"]

    c2q3question = cat_dict["cat2"].json()["clues"][2]["question"]
    c2q3answer = cat_dict["cat2"].json()["clues"][2]["answer"]
    c2q3id = cat_dict["cat2"].json()["clues"][2]["id"]
    c2q3value = cat_dict["cat2"].json()["clues"][2]["value"]

    c2q4question = cat_dict["cat2"].json()["clues"][3]["question"]
    c2q4answer = cat_dict["cat2"].json()["clues"][3]["answer"]
    c2q4id = cat_dict["cat2"].json()["clues"][3]["id"]
    c2q4value = cat_dict["cat2"].json()["clues"][3]["value"]

    c2q5question = cat_dict["cat2"].json()["clues"][4]["question"]
    c2q5answer = cat_dict["cat2"].json()["clues"][4]["answer"]
    c2q5id = cat_dict["cat2"].json()["clues"][4]["id"]
    c2q5value = cat_dict["cat2"].json()["clues"][4]["value"]


    c3title = cat_dict["cat3"].json()['title']
    c3q1question = cat_dict["cat3"].json()["clues"][0]["question"]
    c3q1answer = cat_dict["cat3"].json()["clues"][0]["answer"]
    c3q1id = cat_dict["cat3"].json()["clues"][0]["id"]
    c3q1value = cat_dict["cat3"].json()["clues"][0]["value"]

    c3q2question = cat_dict["cat3"].json()["clues"][1]["question"]
    c3q2answer = cat_dict["cat3"].json()["clues"][1]["answer"]
    c3q2id = cat_dict["cat3"].json()["clues"][1]["id"]
    c3q2value = cat_dict["cat3"].json()["clues"][1]["value"]

    c3q3question = cat_dict["cat3"].json()["clues"][2]["question"]
    c3q3answer = cat_dict["cat3"].json()["clues"][2]["answer"]
    c3q3id = cat_dict["cat3"].json()["clues"][2]["id"]
    c3q3value = cat_dict["cat3"].json()["clues"][2]["value"]

    c3q4question = cat_dict["cat3"].json()["clues"][3]["question"]
    c3q4answer = cat_dict["cat3"].json()["clues"][3]["answer"]
    c3q4id = cat_dict["cat3"].json()["clues"][3]["id"]
    c3q4value = cat_dict["cat3"].json()["clues"][3]["value"]

    c3q5question = cat_dict["cat3"].json()["clues"][4]["question"]
    c3q5answer = cat_dict["cat3"].json()["clues"][4]["answer"]
    c3q5id = cat_dict["cat3"].json()["clues"][4]["id"]
    c3q5value = cat_dict["cat3"].json()["clues"][4]["value"]


    c4title = cat_dict["cat4"].json()['title']
    c4q1question = cat_dict["cat4"].json()["clues"][0]["question"]
    c4q1answer = cat_dict["cat4"].json()["clues"][0]["answer"]
    c4q1id = cat_dict["cat4"].json()["clues"][0]["id"]
    c4q1value = cat_dict["cat4"].json()["clues"][0]["value"]

    c4q2question = cat_dict["cat4"].json()["clues"][1]["question"]
    c4q2answer = cat_dict["cat4"].json()["clues"][1]["answer"]
    c4q2id = cat_dict["cat4"].json()["clues"][1]["id"]
    c4q2value = cat_dict["cat4"].json()["clues"][1]["value"]

    c4q3question = cat_dict["cat4"].json()["clues"][2]["question"]
    c4q3answer = cat_dict["cat4"].json()["clues"][2]["answer"]
    c4q3id = cat_dict["cat4"].json()["clues"][2]["id"]
    c4q3value = cat_dict["cat4"].json()["clues"][2]["value"]

    c4q4question = cat_dict["cat4"].json()["clues"][3]["question"]
    c4q4answer = cat_dict["cat4"].json()["clues"][3]["answer"]
    c4q4id = cat_dict["cat4"].json()["clues"][3]["id"]
    c4q4value = cat_dict["cat4"].json()["clues"][3]["value"]

    c4q5question = cat_dict["cat4"].json()["clues"][4]["question"]
    c4q5answer = cat_dict["cat4"].json()["clues"][4]["answer"]
    c4q5id = cat_dict["cat4"].json()["clues"][4]["id"]
    c4q5value = cat_dict["cat4"].json()["clues"][4]["value"]


    c5title = cat_dict["cat5"].json()['title']
    c5q1question = cat_dict["cat5"].json()["clues"][0]["question"]
    c5q1answer = cat_dict["cat5"].json()["clues"][0]["answer"]
    c5q1id = cat_dict["cat5"].json()["clues"][0]["id"]
    c5q1value = cat_dict["cat5"].json()["clues"][0]["value"]

    c5q2question = cat_dict["cat5"].json()["clues"][1]["question"]
    c5q2answer = cat_dict["cat5"].json()["clues"][1]["answer"]
    c5q2id = cat_dict["cat5"].json()["clues"][1]["id"]
    c5q2value = cat_dict["cat5"].json()["clues"][1]["value"]

    c5q3question = cat_dict["cat5"].json()["clues"][2]["question"]
    c5q3answer = cat_dict["cat5"].json()["clues"][2]["answer"]
    c5q3id = cat_dict["cat5"].json()["clues"][2]["id"]
    c5q3value = cat_dict["cat5"].json()["clues"][2]["value"]

    c5q4question = cat_dict["cat5"].json()["clues"][3]["question"]
    c5q4answer = cat_dict["cat5"].json()["clues"][3]["answer"]
    c5q4id = cat_dict["cat5"].json()["clues"][3]["id"]
    c5q4value = cat_dict["cat5"].json()["clues"][3]["value"]

    c5q5question = cat_dict["cat5"].json()["clues"][4]["question"]
    c5q5answer = cat_dict["cat5"].json()["clues"][4]["answer"]
    c5q5id = cat_dict["cat5"].json()["clues"][4]["id"]
    c5q5value = cat_dict["cat5"].json()["clues"][4]["value"]


    c6title = cat_dict["cat6"].json()['title']
    c6q1question = cat_dict["cat6"].json()["clues"][0]["question"]
    c6q1answer = cat_dict["cat6"].json()["clues"][0]["answer"]
    c6q1id = cat_dict["cat6"].json()["clues"][0]["id"]
    c6q1value = cat_dict["cat6"].json()["clues"][0]["value"]

    c6q2question = cat_dict["cat6"].json()["clues"][1]["question"]
    c6q2answer = cat_dict["cat6"].json()["clues"][1]["answer"]
    c6q2id = cat_dict["cat6"].json()["clues"][1]["id"]
    c6q2value = cat_dict["cat6"].json()["clues"][1]["value"]

    c6q3question = cat_dict["cat6"].json()["clues"][2]["question"]
    c6q3answer = cat_dict["cat6"].json()["clues"][2]["answer"]
    c6q3id = cat_dict["cat6"].json()["clues"][2]["id"]
    c6q3value = cat_dict["cat6"].json()["clues"][2]["value"]

    c6q4question = cat_dict["cat6"].json()["clues"][3]["question"]
    c6q4answer = cat_dict["cat6"].json()["clues"][3]["answer"]
    c6q4id = cat_dict["cat6"].json()["clues"][3]["id"]
    c6q4value = cat_dict["cat6"].json()["clues"][3]["value"]

    c6q5question = cat_dict["cat6"].json()["clues"][4]["question"]
    c6q5answer = cat_dict["cat6"].json()["clues"][4]["answer"]
    c6q5id = cat_dict["cat6"].json()["clues"][4]["id"]
    c6q5value = cat_dict["cat6"].json()["clues"][4]["value"]

    game_id = db.jeopardy.insert({
        'cat1': {'clue1': {'question': c1q1question, 'answer': c1q1answer, 'value': c1q1value, 'id': c1q1id}, 'clue2': {'question': c1q2question, 'answer': c1q2answer, 'value': c1q2value, 'id': c1q2id}, 'clue3': {'question': c1q3question, 'answer': c1q3answer, 'value': c1q3value, 'id': c1q3id}, 'clue4': {'question': c1q4question, 'answer': c1q4answer, 'value': c1q4value, 'id': c1q4id}, 'clue5': {'question': c1q5question, 'answer': c1q5answer, 'value': c1q5value, 'id': c1q5id}, 'category': c1title },
        'cat2': {'clue1': {'question': c2q1question, 'answer': c2q1answer, 'value': c2q1value, 'id': c2q1id}, 'clue2': {'question': c2q2question, 'answer': c2q2answer, 'value': c2q2value, 'id': c2q2id}, 'clue3': {'question': c2q3question, 'answer': c2q3answer, 'value': c2q3value, 'id': c2q3id}, 'clue4': {'question': c2q4question, 'answer': c2q4answer, 'value': c2q4value, 'id': c2q4id}, 'clue5': {'question': c2q5question, 'answer': c2q5answer, 'value': c2q5value, 'id': c2q5id}, 'category': c2title },
        'cat3': {'clue1': {'question': c3q1question, 'answer': c3q1answer, 'value': c3q1value, 'id': c3q1id}, 'clue2': {'question': c3q2question, 'answer': c3q2answer, 'value': c3q2value, 'id': c3q2id}, 'clue3': {'question': c3q3question, 'answer': c3q3answer, 'value': c3q3value, 'id': c3q3id}, 'clue4': {'question': c3q4question, 'answer': c3q4answer, 'value': c3q4value, 'id': c3q4id}, 'clue5': {'question': c3q5question, 'answer': c3q5answer, 'value': c3q5value, 'id': c3q5id}, 'category': c3title },
        'cat4': {'clue1': {'question': c4q1question, 'answer': c4q1answer, 'value': c4q1value, 'id': c4q1id}, 'clue2': {'question': c4q2question, 'answer': c4q2answer, 'value': c4q2value, 'id': c4q2id}, 'clue3': {'question': c4q3question, 'answer': c4q3answer, 'value': c4q3value, 'id': c4q3id}, 'clue4': {'question': c4q4question, 'answer': c4q4answer, 'value': c4q4value, 'id': c4q4id}, 'clue5': {'question': c4q5question, 'answer': c4q5answer, 'value': c4q5value, 'id': c4q5id}, 'category': c4title },
        'cat5': {'clue1': {'question': c5q1question, 'answer': c5q1answer, 'value': c5q1value, 'id': c5q1id}, 'clue2': {'question': c5q2question, 'answer': c5q2answer, 'value': c5q2value, 'id': c5q2id}, 'clue3': {'question': c5q3question, 'answer': c5q3answer, 'value': c5q3value, 'id': c5q3id}, 'clue4': {'question': c5q4question, 'answer': c5q4answer, 'value': c5q4value, 'id': c5q4id}, 'clue5': {'question': c5q5question, 'answer': c5q5answer, 'value': c5q5value, 'id': c5q5id}, 'category': c5title },
        'cat6': {'clue1': {'question': c6q1question, 'answer': c6q1answer, 'value': c6q1value, 'id': c6q1id}, 'clue2': {'question': c6q2question, 'answer': c6q2answer, 'value': c6q2value, 'id': c6q2id}, 'clue3': {'question': c6q3question, 'answer': c6q3answer, 'value': c6q3value, 'id': c6q3id}, 'clue4': {'question': c6q4question, 'answer': c6q4answer, 'value': c6q4value, 'id': c6q4id}, 'clue5': {'question': c6q5question, 'answer': c6q5answer, 'value': c6q5value, 'id': c6q5id}, 'category': c6title },
        'channel': channel,
        'game_active': True,
        'game_started': False,
        'game_host': nick,
    })
    
    
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

    if len(args) > 0 and args[0] == 'game':
        if args[1] == 'new':
            setup_new_game(client, channel, nick, message, cmd, args)


    # if we have an active question, and args, evaluate the answer

    question = mongo_db.find_one({
        'channel': channel,
        'active': True,
        'random': True,
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

    if args[0] == 'game' and args[1] == 'join':
        current_players = []
        current_players.append(new_game["players"])
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
        print new_game["players"]
        client.msg(channel, str(new_game["players"]))
        return
    if question and args:

        logger.debug('found active question')

        correct, partial, ratio = eval_potential_answer(args, question['answer'])

        if correct:

            logger.debug('answer is correct!')

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

        # wrong answer, ignore
        return

    if question and not args:
        logger.debug('no answer provided :/')
        return

    if not question and args:
        logger.debug('no active question :/')
        return

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
