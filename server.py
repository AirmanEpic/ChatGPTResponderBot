from bs4 import BeautifulSoup
import openai
import json
import base64
import hashlib
import boto3
import http
import urllib
import random
import string
import os
import boto3
import datetime
import requests
import time
import re
from functools import reduce

openAISecretKey = #removed for privacy
aws_access_key = os.getenv("AWS_ACCESS_KEY")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")

dyn_resource = boto3.resource('dynamodb')
openai.api_key = openAISecretKey

user_agent_headers = {'User-Agent': 'ResponderAPP_with_AWS/0.1'}

personality_strings = [
    {'name': 'a Robot', 'prompt': "a robot that doesn't say its a robot but suspiciously insists multiple times that it's a human"},
    {'name': 'an alien', 'prompt': "a tentacled alien that says suspicious things a human wouldn't say"},
    {'name': 'a failing magician', 'prompt': "a magician, injecting 'abracadabra', 'poof', and 'ta-da', but messing up his trick at the end."},
    {'name': 'a pirate captain', 'prompt': "A pirate captain during the age of sail who wants to talk about the thread but must mention the rapidly dwindling ration supply."},
    {'name': 'Jay Z', 'prompt': "a very skilled rapper who rhymes everything he says"},
    {'name': 'Dr. Seuss', 'prompt': "Dr. Seuss"},
    {'name': 'Batman', 'prompt': "batman who is trying to be serious but keeps saying 'I'm Batman'"},
    {'name': 'James Bond', 'prompt': "James Bond, injecting a one witty liner about how badass he is every few sentences"},
    {'name': 'Gordon Ramsey', 'prompt': "Gordon Ramsey, very frustrated at a poor cooking job done by in the previous comment"},
]

def lambda_handler(event, context):
    # get the body data
    # Since this is triggered by lambda cloudwatch events, none of this is used.
    print(event)
    code, data = main()
    # add any params to the response
    back_dict = {
        'statusCode': code,
        'body': data
    }
    return back_dict

def getToken(creds):
    urlOptions = {
        'grant_type': 'password',
        'username': creds['username'],
        'password': creds['password']
    }
    credStr = creds['username'] + ':' + creds['password']
    # convert credStr to bytes
    credBytes = credStr.encode('utf-8')
    credentials = base64.b64encode((creds['scriptID'] + ':' + creds['scriptSecret']).encode('utf-8'))
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': 'Basic ' + credentials.decode('utf-8')
    }
    headers = headers.update(user_agent_headers)

    r = requests.post('https://www.reddit.com/api/v1/access_token', data=urlOptions, headers=headers)

    if r.status_code == 200:
        return r.json()['access_token']
    else:
        print('Could not get token', r.status_code, r.text)
        return None

def main():
    bot_creds = {
        # removed for privacy
    }

    # in reddit auth, you first get the auth token using the bot's credentials, then you use that token to get the data you're looking for and post comments
    token = getToken(bot_creds)
    if token is None:
        return 500, 'Could not get token'
    
    # get posts in r/chatGPT using the .json endpoint
    r = requests.get('https://www.reddit.com/r/ChatGPT/.json', headers=user_agent_headers)
    if r.status_code != 200:
        return 500, 'Could not get posts'
    
    # get the posts
    posts = r.json()['data']['children']
    # remove any posts that have already been responded to
    # get posts in which the bot has already commented using pushshift
    r = requests.get('https://api.pushshift.io/reddit/search/comment/?author=GPTGoneResponsive&subreddit=chatGPT', headers=user_agent_headers)
    if r.status_code != 200:    
        return 500, 'Could not get posts bot made'
    
    # get post ids of bot comments
    bot_posts = r.json()['data']
    # get threads of bot posts
    bot_threads = [post['link_id'].split("_")[1] for post in bot_posts]
    print("Bot threads: ", bot_threads)

    # remove any posts that have already been responded to
    posts = [post for post in posts if post['data']['id'] not in bot_threads]

    # remove any posts with less than 20 comments
    posts = [post for post in posts if post['data']['num_comments'] > 20]

    print("Got posts", len(posts))
    if len(posts) == 0:
        return 200, 'No posts to respond to'

    # get a random post in the list.
    post = random.choice(posts)
    print("Getting comments for post", post['data']['id'], 'link: ', 'https://www.reddit.com/r/chatGPT/comments/' + post['data']['id'] + '.json')
    # This function recursively selects and records comments. Due to the way reddit works this must be done in a series of get requests.
    # "Threads" is of the format: [{thread: [comment_ids], parent: parent_id}]
    # "Comments" has the format: {... comment_id: comment ...} where comment is the comment object from the .json endpoint
    threads, comments = get_comment_threads(post)
    # personality is randomly selected from the list of personalities.
    this_personality = random.choice(personality_strings)
    # pick one of the top 4 length posts. Pick from at least 1.
    random_thread_choice = random.choice([i for i in range(max(1,min(len(threads), 4)))])
    # get the thread
    selected_thread = threads[random_thread_choice]
    # the thread must now be turned into a string, looking like this: 
    # "<Comment id> says: <comment text>
    # <Comment id> says: <comment text> etc"
    # this is so that chatgpt can understand the context of the conversation and "Pick" the funniest part to respond to
    # it usually does a DECENT job at this, but it's not perfect. It might be slightly better than random. I know, very scientific.
    choice_prompt = f"""
        In the following conversation thread annotated with comment IDs: 
        {get_thread_content_string_with_ids(selected_thread, comments)} 
        Which comment in the thread would generate the funniest reply if responded to? Reply with only one comment id."""
    
    # get the choice of comment to reply to using OpenAI
    choice = get_choice(choice_prompt)
    if choice is None:
        # sometimes chatgpt gives back no answer at all or fails. In this case, just pick a random comment.
        print("No choice")
        choice = random.choice(selected_thread['thread'])

    # find the ID of "Choice"
    choice_position = None
    if choice in selected_thread['thread']:
        # sometimes the choice is returned as just the comment ID.
        # in this case it's easy.
        choice_position = selected_thread['thread'].index(choice)
    else:
        # sometimes the choice is returned as a comment ID with a bunch of other text.
        # go through each item in the thread and see if it exists in the "choice"
        for thread_id in selected_thread['thread']:
            if thread_id in choice:
                choice_position = selected_thread['thread'].index(thread_id)
                break

        if choice_position is None:
            # if we still can't find the choice, just pick a random comment again.
            print("Lost choice position", choice, selected_thread['thread'])
            choice_position = random.choice(range(len(selected_thread['thread'])))

    # trim the thread to the chosen comment. This means getting rid of the stuff after it.
    selected_thread['thread'] = selected_thread['thread'][:choice_position+1]

    # now we need to run the same operation again, but including the names of the users. This gives chatgpt context on who is saying what.
    response_prompt = f"""
        In the following conversation thread:
        
        {get_thread_content_string(selected_thread, comments)} 
        
        generate a reply to the thread (keeping in mind the context!) with the personality of {this_personality['prompt'] }:"""

    # get the reply from openAI
    reply = get_reply(response_prompt)
    if reply is None:
        return 500, 'Could not get reply'
    
    print("Got reply", reply)

    # this is the full comment, including the bot disclaimer.
    full_comment_message = reply + "\n\n___\n\n This chatbot powered by GPT, replies to threads with different personas. This was "+this_personality['name']+". If anything is weird know that I'm constantly being improved. Please leave feedback!"

    # post the full_comment_message as a reply to the last comment in the selected_thread
    last_comment = selected_thread['thread'][-1]
    print("Posting reply to comment", last_comment)

    # use the reddit api to post the comment
    print("url of comment replying to: ", 'https://www.reddit.com/r/chatGPT/comments/' + post['data']['id'] + '/_/' + last_comment)
    resp = post_comment('ChatGPT', post['data']['id'], last_comment, full_comment_message, token)

    if resp is None:
        return 500, 'Could not post comment'

    return 200, 'Success'

def post_comment(subreddit, post_id, comment_id, text, credentials):

    url = f'https://oauth.reddit.com/r/{subreddit}/api/comment'
    headers = {
        'Authorization': f'Bearer {credentials}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    headers = headers.update(user_agent_headers)
    data = {
        'api_type': 'json',
        'text': text,
        'thing_id': f't1_{comment_id}',
        't3_id': post_id
    }
    r = requests.post(url, headers=headers, data=data)
    print(r)
    print(r.text)
    if r.status_code == 200:
        return r.json()['json']['data']['things'][0]['data']
    else:
        print('Could not post comment', r.status_code, r.text)
        return None

def get_comment_threads(this_post):
    # Top level comments will be the inital recursive seed.
    top_level_comments = get_top_level_comments('chatGPT', this_post['data']['id'])
    # get post title
    title = this_post['data']['title']
    # get ALL comments in the thread. 
    # form a recursive network of the comments in that thread
    # get the comments in the thread
    comments = get_all_comments('chatGPT', this_post['data']['id'], post_title=title.replace(' ', '_'))
    print("Comment count: ", len(comments))
    print("A sample comment", comments[0])
    all_comments = [
        {
            'comment_id': comment['id'] if '_' not in comment['id'] else comment['id'].split('_')[1],
            'parent_id': comment['parent_id'] if '_' not in comment['parent_id'] else comment['parent_id'].split('_')[1],
            'content': comment['body'],
            'user_id': comment['author']
        } for comment in comments]
    if len(all_comments) == 0:
        print(len(comments))
        return 500, 'No comments in thread'
    # dict based lookup table of comments. 
    comment_lut = {comment['comment_id']: comment for comment in all_comments}
    comment_structure = []
    print("all comments count: ", len(all_comments))
    comments_remaining = [comment for comment in all_comments]
    # get the top level comments and make threads based on them
    comment_structure = [{'thread_id': comment['data']['id'], 'thread': [comment['data']['id']]} for comment in top_level_comments]
    # Make the comment structure for the non-top level comments
    count = 0
    print("Comments remaining len: ",len(comments_remaining))
    while len(comments_remaining) > 0 and count < 1000:
        current_comment = comments_remaining[0]
        print("Doing loop! Comments remaining before: ", len(comments_remaining), "current comment: ", current_comment['comment_id'], "parent: ", current_comment['parent_id'])
        packt = make_replyset_for(comment_structure, current_comment['comment_id'], comments_remaining, comment_lut, this_post['data']['id'])
        if packt is not None:
            comment_structure, comments_remaining = packt
        count += 1
        if (count > 998):
            print('Could not make comment structure')
            return 500, 'Could not make comment structure'
    # print the first thread for debugging purposes
    # sort the threads by length, highest length first
    comment_structure = sorted(comment_structure, key=lambda x: len(x['thread']), reverse=True)
    print(comment_structure)
    return comment_structure, comment_lut

def make_replyset_for(start_structure, id_of_comment, remaining_comments, lut, post_id):
    # find the comment in the remaining_comments list. 
    if ("_" in id_of_comment):
        id_of_comment = id_of_comment.split("_")[1]
    comment = lut[id_of_comment]

    if comment['parent_id'] == post_id:
        # this comment is a top level comment
        # remove it from the remaining_comments list
        remaining_comments.remove(comment)
        return start_structure, remaining_comments
    print("Current start_structure: ", start_structure)
    # see if that comment has a parent in the start_structure's thread property
    for thread in start_structure:
        # check to see if this thread has the parent
        try:
            parent_position = thread['thread'].index(comment['parent_id'])
        except ValueError:
            continue
        
        # check if the parent is the last comment in the thread
        print("parent_position: ", parent_position, "thread length: ", len(thread['thread']))
        if parent_position == len(thread['thread']) - 1:
            # add the comment to the thread
            thread['thread'].append(comment['comment_id'])
            # remove the comment from the remaining_comments list
            remaining_comments.remove(comment)
            return start_structure, remaining_comments
        else:
            # make a copy of the thread up until and including the parent
            new_thread = {'thread_id': comment['comment_id'], 'thread': thread['thread'][:parent_position + 1]}
            # add the comment to the new thread
            new_thread['thread'].append(comment['comment_id'])
            # remove the comment from the remaining_comments list
            remaining_comments.remove(comment)
            # add the new thread to the start_structure
            start_structure.append(new_thread)
            return start_structure, remaining_comments

def get_all_comments(subreddit, post_id, comment_list=None, post_title='', comment_id=''):
    headers = user_agent_headers
    if comment_list is None:
        comment_list = []

    if comment_id == '':
        url = f'https://www.reddit.com/r/{subreddit}/comments/{post_id}.json'
        r = requests.get(url, headers=headers)
        comments_data = r.json()[1]['data']['children']
    else:
        print("Comment id: ",comment_id, len(comment_id))
        # remove "/" from the post_title
        post_title_temp = post_title.replace('/', '')
        post_title_temp = post_title_temp.replace(',', '')
        post_title_temp = post_title_temp.replace("'", '')
        url = f'https://www.reddit.com/r/{subreddit}/comments/{post_id}/{post_title_temp}/{comment_id}.json'
        print("Getting url ",url)
        r = requests.get(url, headers=headers)
        
        try: 
            comments_data = r.json()[1]['data']['children'][0]['data']['replies']['data']['children']
        except Exception as e:
            print("Error: ", e)
            print("Response: ", r.text)
            return comment_list
    
    # trim comments_data to a max length of 3
    print("Comments data length: ", len(comments_data))
    comments_data = comments_data[:3]
    print("Comments data length: ", len(comments_data))

    for comment in comments_data:
        comment_data = comment['data']
        comment_list.append(comment_data)

        if comment_data.get('replies'):
            get_all_comments(subreddit, post_id, comment_list=comment_list, post_title=post_title, comment_id=comment_data['id'])

    return comment_list

def get_top_level_comments(subreddit, postid):
    r = requests.get(f'https://www.reddit.com/r/{subreddit}/comments/{postid}.json', headers=user_agent_headers)
    comments = r.json()[1]['data']['children']
    return comments

def get_reply(prompt):
    try:
        response = openai.Completion.create(
            engine="text-davinci-003",
            prompt=prompt,
            temperature=0.9,
            max_tokens=300,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0.6
        )
    except Exception as e:
        print("Error! ", e)
        return None
    response_raw = response['choices'][0]['text']
    # trim the quotes off of the response if there
    if response_raw[0] == '"':
        response_raw = response_raw[1:]
    if response_raw[-1] == '"':
        response_raw = response_raw[:-1]

    # duplicate any newlines in the response so they are preserved
    response_raw = response_raw.replace('\n', '\n\n')
    return response_raw

def get_choice(prompt):
    try:
        # use a weaker engine as this is a fairly simple problem.
        response = openai.Completion.create(
            engine="text-curie-001",
            prompt=prompt,
            temperature=0.9,
            max_tokens=300,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0.6
        )
    except Exception as e:
        print("Error! ", e)
        return None
    response_raw = response['choices'][0]['text']
    return response_raw

def get_thread_content_string(thread, comments):
    content = ''
    for i, comment_id in enumerate(thread['thread']):
        print("Comment id: ", comment_id)
        this_comment = comments[comment_id]
        content += f'{this_comment["user_id"]} says: {this_comment["content"]} \n'

    return content

def get_thread_content_string_with_ids(thread, comments):
    content = ''
    for i, comment_id in enumerate(thread['thread']):
        print("Comment id: ", comment_id)
        this_comment = comments[comment_id]
        content += f'{comment_id}: {this_comment["content"]} \n'

    return content