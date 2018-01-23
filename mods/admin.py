import argparse
import io
import subprocess
import sys
import re
import textwrap
import time
import traceback
from contextlib import redirect_stdout
from typing import Tuple

import ujson
import requests
from disco.bot import Plugin, CommandLevels
from disco.bot.command import CommandEvent
from disco.api.http import APIException


def _convert_to_bool(argument):
    lowered = argument.lower()
    if lowered in ('yes', 'y', 'true', 't', '1', 'enable', 'on'):
        return True
    elif lowered in ('no', 'n', 'false', 'f', '0', 'disable', 'off'):
        return False
    else:
        raise ValueError(lowered + ' is not a recognised boolean option')


# noinspection PyBroadException
class Admin(Plugin):
    # def get_level(self, wew, actor):
    #     if str(actor.id) in self.bot.config.levels:
    #         return self.bot.config.levels[str(actor.id)]
    def __init__(self, *args):
        super(Admin, self).__init__(*args)
        self.repl_sessions = set()
        self.session = requests.Session()
        self._last_result = None

    @staticmethod
    def cleanup_code(content):
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # remove `foo`
        return content.strip('` \n')

    @staticmethod
    def get_syntax_error(e):
        return '```py\n{0.text}{1:>{0.offset}}\n{2}: {0}```'.format(e, '^', e.__class__.__name__)

    def wait_for_message(self, author=None, channel=None, content=None, check=None):
        def predicate(message):
            result = True
            if author:
                result = result and message.author == author

            if content:
                result = result and message.content == content

            if channel:
                result = result and message.channel.id == channel.id

            if callable(check):
                result = result and check(message)

            return result

        try:
            while True:
                msg = self.wait_for_event('MessageCreate').get()
                if predicate(msg) is True:
                    return msg
        except Exception as e:
            print(type(e).__name__ + ': ' + str(e))

    @staticmethod
    def settings_parser(text: str = None) -> dict:
        parse = argparse.ArgumentParser(add_help=False)
        parse.add_argument('--set-custom-prefix', type=str)
        parse.add_argument('--remove-custom-prefix', action='store_true')
        parse.add_argument('--audio-volume', type=int)
        parse.add_argument('--ignore-server', type=_convert_to_bool)
        parse.add_argument('--ignore-channel', type=_convert_to_bool)
        parse.add_argument('--send-mcfm-messages', type=_convert_to_bool)
        parse.add_argument('--minimum-votes-to-skip', type=int)

        def error(message: str) -> dict:
            return {'error': message}

        parse.error = error
        args = parse.parse_known_args(text.split())
        return args if not args else (args[0].__dict__ if [attr for attr in args[0].__dict__.keys() if getattr(args[0], attr) is not None] else None)

    def create_gist(self, description: str, eval_in: str, eval_out: str, eval_time: Tuple[float, float], in_format: str = 'py'):
        headers = {
            'Accept': 'application/vnd.github.v3.full+json',
            'Accept-Charset': 'utf-8',
            'Content-Type': 'application/json',
            'Authorization': 'token ' + self.bot.credentials["github"]["token"]
        }
        payload = {
            'description': description,
            'public': False,
            'files': {
                'eval_input_{}.{}'.format(round(eval_time[0]), in_format): {
                    'content': eval_in
                },
                'eval_output_{}.txt'.format(round(eval_time[1])): {
                    'content': eval_out
                }
            }
        }
        response = self.session.request('POST', 'https://api.github.com/gists', data=ujson.dumps(payload, ensure_ascii=True), headers=headers)
        return response.json()['html_url']

    def send_output(self, event: CommandEvent, eval_in: str, eval_out: str, eval_time: Tuple[float, float], in_format: str = 'py'):
        content = '```{}\n{}\n```'.format(in_format, eval_out)
        if len(content) > 2000:
            url = self.create_gist('Evaluation for ' + self.client.state.me.username, eval_in, eval_out, eval_time, in_format)
            event.msg.reply('Content too big to be printed, use this link to see the result: ' + url)
        else:
            event.msg.reply(content)

    @Plugin.command('debug', '<code:str...>', aliases=['eval'], level=CommandLevels.OWNER)
    def debug(self, event: CommandEvent, code: str):
        _time = time.time()
        env = {
            'event': event,
            'bot': self.bot,
            'client': self.client,
            'message': event.msg,
            'guild': event.msg.guild,
            'channel': event.msg.channel,
            'author': event.msg.author,
            '_': self._last_result
        }

        env.update(globals())

        body = self.cleanup_code(code)
        stdout = io.StringIO()
        to_compile = 'def func():\n{}'.format(textwrap.indent(body, "  "))

        try:
            exec(to_compile, env)
        except Exception:
            return event.msg.reply('**Something happened**\n```py\n{}\n```'.format(traceback.format_exc()))

        # noinspection PyUnresolvedReferences
        func = env['func']
        try:
            with redirect_stdout(stdout):
                ret = func()
        except Exception:
            value = stdout.getvalue()
            self.send_output(event, body, '{}{}'.format(value, traceback.format_exc()), (_time, time.time()))
        else:
            value = stdout.getvalue()
            if ret is None:
                if value:
                    self.send_output(event, body, value, (_time, time.time()))
            else:
                self._last_result = ret
                self.send_output(event, body, '{}{}'.format(value, ret), (_time, time.time()))

    @Plugin.command('bash', '<cmd:str...>', aliases=['sh', 'terminal'], level=CommandLevels.OWNER)
    def bash(self, event: CommandEvent, cmd: str):
        """Interaction with the GNU/Linux environment."""
        code = self.cleanup_code(cmd)
        _time = time.time()
        process = subprocess.Popen(code.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        out, err = process.communicate()
        rec = re.compile(r'(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]')
        out, err = rec.sub('', out.decode('utf-8')), rec.sub('', err.decode('utf-8'))
        if err:
            return self.send_output(event, code, err, (_time, time.time()), 'sh')
        self.send_output(event, code, out, (_time, time.time()), 'sh')

    @Plugin.command('repl', level=CommandLevels.OWNER)
    def repl(self, event):
        msg = event.msg
        code = None
        variables = {
            'event': event,
            'bot': self.bot,
            'client': self.client,
            'message': msg,
            'guild': msg.guild,
            'channel': msg.channel,
            'author': msg.author,
            '_': self._last_result,
        }

        if msg.channel.id in self.repl_sessions:
            event.msg.reply('Already running a REPL session in this channel. Exit it with `quit`.')
            return

        self.repl_sessions.add(msg.channel.id)
        msg.reply('Python {} on {}\nEnter code to execute or evaluate. `exit()` or `quit` to exit.'.format(sys.version.replace('\n', ''), sys.platform))
        while True:
            response = self.wait_for_message(author=msg.author, channel=msg.channel, check=lambda m: m.content.startswith('`') and m.content.endswith('`'))
            cleaned = self.cleanup_code(response.content)

            if cleaned in ('quit', 'exit', 'exit()'):
                msg.reply('Exiting.')
                self.repl_sessions.remove(msg.channel.id)
                return

            executor = exec
            if cleaned.count('\n') == 0:
                # single statement, potentially 'eval'
                try:
                    code = compile(cleaned, '<repl session>', 'eval')
                except SyntaxError:
                    pass
                else:
                    executor = eval

            if executor is exec:
                try:
                    code = compile(cleaned, '<repl session>', 'exec')
                except SyntaxError as e:
                    msg.reply(self.get_syntax_error(e))
                    continue

            variables['message'] = response
            fmt = None
            stdout = io.StringIO()

            try:
                with redirect_stdout(stdout):
                    result = executor(code, variables)
            except Exception:
                value = stdout.getvalue()
                fmt = '```py\n{}{}\n```'.format(value, traceback.format_exc())
            else:
                value = stdout.getvalue()
                if result is not None:
                    fmt = '```py\n{}{}\n```'.format(value, result)
                    variables['_'] = result
                elif value:
                    fmt = '```py\n{}\n```'.format(value)

            try:
                if fmt is not None:
                    if len(fmt) > 2000:
                        event.msg.reply('Content too big to be printed.')
                    else:
                        event.msg.reply(fmt)
            except APIException as e:
                event.msg.reply('Unexpected error: `{}`'.format(e))

    @Plugin.command('quit', aliases=['exit', 'logout', 'shutdown'], level=CommandLevels.OWNER)
    def quit(self, event: CommandEvent):
        event.msg.reply("Shutting Down!")
        self.bot.connect_py.close()
        self.bot.rethink.close()
        self.client.gw.ws_event.set()
