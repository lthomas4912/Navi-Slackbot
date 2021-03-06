import os
import re
import time
import json
from datetime import datetime
from typing import List, Dict, Set, Iterator
import requests
from bs4 import BeautifulSoup
from slackclient import SlackClient
from simplegist.simplegist import Simplegist

sections = {"git": "GitHub", "stackoverflow": "StackOverflow", "java": "Java", "python": "Python",
            "interview": "Interview", "": "Misc"}
ignored_titles: List[str] = ["not found", "forbidden", "denied"]
slack_token: str = os.environ["OAUTH_ACCESS_TOKEN"]
slack_client: SlackClient = SlackClient(slack_token)
gist_list_id = "af088f66c27df3e6462a6cd0f2a9071c"
gist_find_all = "4a06315bf0a5593b9ff2456bcb7ef5fb"


class Link:
    def __init__(self, url, creator, timestamp):
        self.url = url
        self.creator = creator
        self.timestamp = timestamp

    def __key(self):
        return self.url, self.creator, self.timestamp

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        return isinstance(self, type(other)) and self.__key() == other.__key()

    def to_json(self):
        return {
            'url': self.url,
            'creator': self.creator,
            'timestamp': self.timestamp
        }

    @classmethod
    def from_json(cls, data):
        return cls(data['url'],
                   data['creator'],
                   data['timestamp'])


# Get users for mapping onto their ids
def get_users():
    raw_users = slack_client.api_call("users.list")
    users = {}
    for user in raw_users['members']:
        if 'profile' in user:
            users[user['id']] = user['profile']['real_name']
    return users


def get_all_links(channel_id):
    """
    Reads in and saves all links from a channel's history

    :param gist_id:
    :param channel_id:
    :return:
    """
    has_more = True
    latest = datetime.now().timestamp()
    call = "channels.history" if channel_id[0] == "C" else "groups.history"
    while has_more:
        current = slack_client.api_call(call, channel=channel_id, latest=latest, count=200)
        if not current['ok'] and current['error'] == 'ratelimited':
            time.sleep(int(current['headers']['Retry-After']))
        else:
            yield get_links(current['messages'])
            has_more = current['has_more']
            latest = current['messages'][len(current['messages'])-1]['ts']


# get link from message text
def parse_message(message):
    message_text = message['text']
    if is_link(message_text):
        url = message_text[message_text.index('<') + 1:message_text.index('>')]
        return [Link(url, message['user'], message['ts'])]


def is_link(text):
    if len(text) > 0 and '<' in text and text[text.index('<') + 1] == 'h' and 'gist.github.com/ElBell' not in text:
        return True
    return False


# get links from attachments
def parse_attachments(message):
    attachment_links = []
    for attachment in message['attachments']:
        if 'original_url' in attachment:
            attachment_links.append(Link(attachment['original_url'], message['user'], message['ts']))
        if 'app_unfurl_url' in attachment:
            attachment_links.append(Link(attachment['app_unfurl_url'], message['user'], message['ts']))
    return attachment_links


def sort_into_sections(links_set: Set[Link]):
    sectioned_links = {sections[section]: [] for section in sections.keys()}
    for link in links_set:
        sectioned_links[get_section(link)].append(link)
    return sectioned_links


def get_section(link):
    for key, title in sections.items():
        if key in link.url:
            return title


def link_or_attachment(raw_message):
    if 'attachments' in raw_message or is_link(raw_message):
        return True
    return False


# Gets all links from raw_messages and writes out to the proper gist
def get_links(raw_messages):
    links_set: Set[Link] = set()
    for message in [raw_message for raw_message in raw_messages if 'navi' not in str(raw_message).lower()]:
        links: List[Link] = parse_link_or_attachment(message)
        if links is not None:
            links_set.update(links)
    return sort_into_sections(links_set)


# gets json_data for a group of links
def merge_dicts(links):
    total_links = {}
    for link in links:
        for title in link:
            if title not in total_links:
                total_links[title] = []
            total_links[title].extend(link[title])
    return total_links


def generate_json(gist_id, new_links):
    gist = Simplegist(username='ElBell', api_token=os.environ["GIST_ACCESS_TOKEN"])
    json_data = json.loads(gist.profile().content(id=gist_id))
    old_links = {category: [Link.from_json(link) for link in links] for category, links in json_data.items()}
    total_links = merge_dicts([old_links, sort_into_sections(new_links)])
    json_data = {category: [link.to_json() for link in links] for category, links in total_links.items()}
    # updates the json
    gist.profile().edit(id=gist_id, content=json.dumps(json_data))
    return total_links


# Returns channel name from channel id
def get_channel_name(channel_id):
    if channel_id[0] == "C":
        return slack_client.api_call("channels.info", channel=channel_id)["channel"]["name"]
    else:
        return slack_client.api_call("groups.info", channel=channel_id)["group"]["name"]


# Makes link or attachment into Link objects
def parse_link_or_attachment(message: str) -> List[Link]:
    if 'attachments' in message:
        return parse_attachments(message)
    elif 'text' in message:
        return parse_message(message)


# the json of Link objects in read in and the new links are inserted in the proper location
def add_link(message, channel_id):
    gist = Simplegist(username='ElBell', api_token=os.environ["GIST_ACCESS_TOKEN"])
    keys = json.loads(gist.profile().content(id=gist_list_id))[channel_id]
    new_links: List[Link] = parse_link_or_attachment(message)
    total_links = generate_json(keys[0], new_links)
    # updates the markdown
    gist.profile().edit(id=keys[1], content=generate_md_file(total_links, channel_id))


# Puts links into their proper section based on url contents
def add_to_section(links, sectioned_links):
    for link in links:
        for key, title in sections.items():
            if key in link.url:
                sectioned_links[title].append(link)
    return sectioned_links


def empty_section_dict():
    empty_dict = {}
    for title in sections.values():
        empty_dict[title] = []
    return json.dumps(empty_dict)


def get_history(channel_id):
    gist = Simplegist(username='ElBell', api_token=os.environ["GIST_ACCESS_TOKEN"])
    total_links = merge_dicts(get_all_links(channel_id))
    json_data = {category: [link.to_json() for link in links] for category, links in total_links.items()}
    # updates the json
    json_file = gist.create(name=get_channel_name(channel_id) + ".json", description="json for channel links",
                            content=json.dumps(json_data))
    md_file = gist.create(name=get_channel_name(channel_id) + ".md", description="Collected links of channel",
                          content=generate_md_file(total_links, channel_id))
    keys = json.loads(gist.profile().content(id=gist_list_id))
    keys[channel_id] = [json_file['id'], md_file['id']]
    gist.profile().edit(id=gist_list_id, content=json.dumps(keys))
    generate_findall()
    return md_file['Gist-Link']


# Getting the possible title from the link using Beautiful Soup
def generate_link_md(link: Link, users):
    try:
        response = requests.get(link.url)
        if response.ok:
            regex = re.compile('<title>(.*?)</title>', re.IGNORECASE | re.DOTALL)
            title = regex.search(response.text).group(1)
            if len(title) < 3:
                title = link.url
        else:
            title = link.url
    except:
        title = link.url
    title = re.sub(r"[\n\t]*", "", title).strip()
    return f"[{title.strip()}]({link.url})<br/>By: {users[link.creator]} " \
        f"Posted: {datetime.fromtimestamp(float(link.timestamp)).strftime('%b %d %Y %I:%M:%S%p')} <br/> "


def generate_md_file(sectioned_links, channel_id):
    users = get_users()
    md_file = [f"# {get_channel_name(channel_id)}"]
    for title, links in sectioned_links.items():
        if len(links) > 0:
            md_file.append(f"\n## {title}<br/>\n")
            links.sort(key=lambda x: x.timestamp)
            for link in links:
                md_file.append(generate_link_md(link, users))
    return ''.join(md_file)


# Creates a markdown that has a list of all the channel collections Navi has
def generate_findall():
    gist = Simplegist(username='ElBell', api_token=os.environ["GIST_ACCESS_TOKEN"])
    keys = json.loads(gist.profile().content(id=gist_list_id))
    gist.profile().edit(id=gist_find_all, content=generate_findall_md(keys))


# Creates generates the actual markdown for generate_findall
def generate_findall_md(keys):
    file = []
    for channel_id in sorted(keys.keys()):
        file.append(f"[{get_channel_name(channel_id)}](https://gist.github.com/ElBell/{keys[channel_id][1]})<br/>")
    return "# .All links<br/>\n" + ''.join(sorted(file))


# Links to specific channel's gist
def get_link_to_links(channel_id):
    gist = Simplegist(username='ElBell', api_token=os.environ["GIST_ACCESS_TOKEN"])
    keys = json.loads(gist.profile().content(id=gist_list_id))
    return f"https://gist.github.com/ElBell/{keys[channel_id][1]}"


# Links to all of Navi's gists by channel name
def get_link_to_all():
    return f"https://gist.github.com/ElBell/{gist_find_all}"


