import argparse
import datetime
import logging
import ujson

import connect
import psutil
import rethinkdb as r
from disco.bot import Bot, BotConfig
from disco.bot.command import CommandEvent
from disco.client import Client, ClientConfig
from disco.gateway.events import MessageCreate
from disco.types.permissions import Permissions
from disco.util.logging import setup_logging
from gevent import monkey

monkey.patch_all()

parser = argparse.ArgumentParser()
parser.add_argument('--log-level', help='log level', default=None)
with open('credentials.json') as file:
    credentials = ujson.load(file)


class ConnectBot(Bot):
    def __init__(self, *args, **kwargs):
        super(ConnectBot, self).__init__(*args, **kwargs)
        self.credentials = credentials
        r.set_loop_type('gevent')
        self.rethink = r.connect(host=self.credentials['rethink_db']['ip'], port=self.credentials['rethink_db']['port'],
                                 password=self.credentials['rethink_db']['password'], db='connect')
        self.connect_py = connect.Client()
        self.up_time = datetime.datetime.utcnow()
        self.process = psutil.Process()

    def get_commands_for_message(self, require_mention, mention_rules, prefix, msg) -> list:
        content = msg.content
        mention_check = False

        if require_mention:
            mention_direct = msg.is_mentioned(self.client.state.me)
            mention_everyone = msg.mention_everyone

            mention_roles = []
            if msg.guild:
                mention_roles = list(filter(lambda _role: msg.is_mentioned(_role),
                                            msg.guild.get_member(self.client.state.me).roles))
            mention_check = any((
                    mention_rules.get('user', True) and mention_direct,
                    mention_rules.get('everyone', False) and mention_everyone,
                    mention_rules.get('role', False) and any(mention_roles),
                    msg.channel.is_dm,
            ))
            if not mention_check and not prefix:
                return []

            if mention_direct:
                if msg.guild:
                    member = msg.guild.get_member(self.client.state.me)
                    if member:
                        # If nickname is set, filter both the normal and nick mentions
                        if member.nick:
                            content = content.replace(member.mention, '', 1)
                        content = content.replace(member.user.mention, '', 1)
                else:
                    content = content.replace(self.client.state.me.mention, '', 1)
            elif mention_everyone:
                content = content.replace('@everyone', '', 1)
            else:
                for role in mention_roles:
                    content = content.replace('<@{}>'.format(role), '', 1)

            content = content.lstrip()

        if all((require_mention, prefix, not mention_check, not content.startswith(prefix))):
            return []
        else:
            if content.startswith(prefix) and not mention_check:
                content = content[len(prefix):]

        if not self.command_matches_re or not self.command_matches_re.match(content):
            return []

        options = []
        for command in self.commands:
            match = command.compiled_regex.match(content)
            if match:
                options.append((command, match))
        return sorted(options, key=lambda obj: obj[0].group is None)

    def handle_message(self, msg, custom_prefix=None) -> bool:
        commands = list(self.get_commands_for_message(
            self.config.commands_require_mention,
            self.config.commands_mention_rules,
            self.config.commands_prefix if not custom_prefix else custom_prefix,
            msg,
        ))

        if not len(commands):
            return False

        for command, match in commands:
            if not self.check_command_permissions(command, msg):
                continue

            if command.plugin.execute(CommandEvent(command, msg, match)):
                return True
        return False

    def on_message_create(self, event: MessageCreate):
        if event.message.author.id == self.client.state.me.id:
            return
        if event.author.bot:
            return
        if event.channel.is_guild:
            _payload = r.table('settings').get(event.guild.id >> 22).run(self.rethink)
            payload = {
                'id': event.guild.id >> 22, 'volume': 0.7, 'min_skips': 3, 'send_mcfm_messages': True,
                'prefix': None, 'ignore_guild': False, 'ignored_channels': []
            } if not _payload else _payload
            if event.guild.id >> 22 == payload['id']:
                member_roles = [event.guild.roles[rid].name for rid in event.message.member.roles]
                check_for_role = connect.utils.find(lambda role: role in ('Bot Mod', 'Mod', 'Mods', 'Moderator', 'Moderators'), member_roles)
                check = not (event.author.id == 201742045952344064) and not check_for_role and not event.message.member.permissions.can(Permissions.MANAGE_MESSAGES)
                if payload['ignore_guild'] and check:
                    return
                elif (event.channel.id >> 22) in payload['ignored_channels'] and check:
                    return
            if not _payload:
                r.table('settings').insert(payload).run(self.rethink)
            result = self.handle_message(event.message, payload['prefix'])
        else:
            result = self.handle_message(event.message)

        if self.config.commands_allow_edit:
            self.last_message_cache[event.message.channel_id] = (event.message, result)


def disco_main():
    """
    Creates an argument parser and parses a standard set of command line arguments,
    creating a new :class:`Client`.

    Returns
    -------
    :class:`Client`
        A new Client from the provided command line arguments
    """
    args = parser.parse_args()

    config = ClientConfig.from_file('config.yaml')
    config.token = credentials['discord_login']['beta']
    if args.log_level:
        config.log_level = args.log_level

    setup_logging(level=getattr(logging, config.log_level.upper()))

    bot_config = BotConfig(config.bot)
    bot = ConnectBot(Client(config), bot_config)
    bot.run_forever()


if __name__ == '__main__':
    disco_main()
