#!/usr/local/bin/python
from os import environ
environ['NCORE_CONFIG'] = '/home/synack/src/ircbnc/'

from ncore.config import conf
from socket import socket, SO_REUSEADDR, SOL_SOCKET
from traceback import format_exc
from ssl import wrap_socket
from urllib import urlopen
from Queue import Queue
from select import select
from time import sleep
import json
import re

class Client(object):
    def __init__(self, address, nick, password=None):
        self.address = tuple(address)
        self.nick = nick
        self.mask = ''
        self.password = password
        self.sock = socket()
        self.buffer = ''

        self.motd = []
        self.channels = {}
        self.modes = {}

    def readlines(self, delim='\n'):
        while True:
            block = self.sock.recv(1024)
            if not block:
               break
            self.buffer += block
            while self.buffer.find(delim) != -1:
                line, self.buffer = self.buffer.split(delim, 1)
                yield line

    def send(self, line):
        if not line.endswith('\n'):
            line += '\n'
        while line:
            bytes = self.sock.send(line)
            line = line[bytes:]

    def connect(self):
        self.sock.connect((self.address))
        self.send('USER %s 2 %s :%s' % (self.nick, self.nick, self.nick))
        if self.password:
            self.send('PASS %s' % self.password)
        self.send('NICK %s' % self.nick)

        for line in self.readlines():
            self.motd.append(line)
            if line.split(' ', 2)[1] == '376': break
            if line.split(' ', 2)[1] == '001': self.irc_001(line)

    def update(self):
        if not select([self.sock], [], [], 0.01)[0]: return
        self.buffer += self.sock.recv(1024)
        while self.buffer.find('\n') != -1:
            line, self.buffer = self.buffer.split('\n', 1)
            line = line.rstrip('\r')
            if line.startswith('PING'):
                self.send('PONG %s' % line.split(' ', 1)[1])
                continue
            self.relay_line(line)

            if line.startswith(':'):
                mask, message, params = line.split(' ', 2)
                mask = mask.lstrip(':')
            else:
                message, params = line.split(' ', 1)
                mask = None

            methodname = 'irc_%s' % message
            if hasattr(self, methodname):
                method = getattr(self, methodname)
                method(mask, params)

    def relay_line(self, line): pass

    def irc_JOIN(self, mask, params):
        chan = params.lstrip(':')
        if not chan in self.channels:
            self.channels[chan] = []
        if not mask in self.channels[chan]:
            self.channels[chan].append(mask)

    def irc_353(self, mask, params):
        nick, params = params.split(' = ', 1)
        chan, params = params.split(' :', 1)

        for nick in params.split(' '):
            self.channels[chan].append(nick)

    def irc_MODE(self, mask, params):
        params = params.split(' ')
        if len(params) != 3: return
        chan, mode, nick = params
        if not chan in self.modes:
            self.modes[chan] = {}
        if not nick in self.modes[chan]:
            self.modes[chan][nick] = []
        for c in mode.lstrip('+-'):
            if not c in self.modes[chan][nick] and mode.startswith('+'):
                self.modes[chan][nick].append(c)
            if c in self.modes[chan][nick] and mode.startswith('-'):
                self.modes[chan][nick].remove(c)

    def irc_PART(self, mask, params):
        chan, nicks = params.split(' :', 1)
        if chan in self.channels:
            del self.channels[chan]

    def irc_001(self, line):
        self.mask = line.rsplit(' ', 1)[1].rstrip('\r')

class RelayClient(object):
    def __init__(self, address, sock, upstream):
        self.address = '%s:%i' % address
        self.sock = sock
        self.upstream = upstream
        self.buffer = ''
        self.push = []
        self.state = 'init'

        self.user = None
        self.nick = None
        self.password = None

        self.sendq = Queue()

    def send(self, line):
        print '%s << %s' % (self.address.ljust(25, ' '), line.rstrip('\r\n'))
        while line:
            bytes = self.sock.send(line)
            line = line[bytes:]

    def update(self):
        method = getattr(self, 'state_%s' % self.state)
        method()

    def state_init(self):
        if not select([self.sock], [], [], 0.01)[0]: return

        self.buffer += self.sock.recv(1024)
        while self.buffer.find('\n') != -1:
            line, self.buffer = self.buffer.split('\n', 1)
            if not line: continue
            line = line.rstrip('\r')
            print '%s >> %s' % (self.address.ljust(25, ' '), line)
            if line.find(' ') == -1:
                self.sock.close()
                self.state = 'closed'
                return
            message, params = line.split(' ', 1)
            if message == 'USER':
                self.user = params.split(' ', 3)
            if message == 'PASS':
                self.password = params
            if message == 'NICK':
                self.nick = params
                self.state = 'motd'

    def state_motd(self):
        if not select([], [self.sock], [], 0.01)[1]: return

        motd = '\n'.join(self.upstream.motd) + '\n'
        self.send(motd)

        self.state = 'relay'

    def state_relay(self):
        readable, writable = select([self.sock], [self.sock], [], 0.01)[:2]
        if not self.sendq.empty() and writable:
            line = self.sendq.get()
            self.send(line)

        if readable:
            block = self.sock.recv(1024)
            if not block:
                self.state = 'closed'
                return
            self.buffer += block

            while self.buffer.find('\n') != -1:
                line, self.buffer = self.buffer.split('\n', 1)
                print '%s >> %s' % (self.address.ljust(25, ' '), line.rstrip('\r\n'))

                if line.find(' ') != -1:
                    message, params = line.split(' ', 1)
                    methodname = 'irc_' + message
                    if hasattr(self, methodname):
                        method = getattr(self, methodname)
                        method(params.rstrip('\r'))
                        continue
                self.relay_line(line)

    def state_closed(self):
        self.close(self)

    def close(self, ref): pass
    def relay_line(self, line): pass

    def irc_QUIT(self, params):
        self.sock.close()
        self.state = 'closed'

    def irc_PUSH(self, params):
        self.push.append(params)
        if params.startswith('end-device'):
            self.register_push(self.push)
            self.push = []
        if params.startswith('remove-device'):
            self.delete_push(params)

    def irc_JOIN(self, params):
        if params in self.upstream.channels:
            nicks = ' '.join([x.split('!', 1)[0] for x in self.upstream.channels[params] if x])
            self.send(':%s JOIN :%s\n' % (self.upstream.mask, params))
            self.send(':localhost 353 %s = %s :%s\n' % (self.nick, params, nicks))
            self.send(':localhost 366 %s %s :End of NAMES list\n' % (self.nick, params))
            for nick in self.upstream.modes.get(params, []):
                modes = self.upstream.modes[params][nick]
                for c in modes:
                    self.send(':localhost MODE %s +%s %s\n' % (params, c, nick))
        else:
            self.relay_line('JOIN ' + params)

class PushServer(object):
    def __init__(self, server):
        self.sendq = Queue()
        self.devices = {}
        self.server = server

    def register(self, messages):
        device = {'highlight-word': []}
        token = None
        for line in messages:
            line = line.rstrip('\r\n')
            if line == 'end-device':
                self.devices[token] = device
                continue
            action, params = line.split(' ', 1)
            if action == 'add-device':
                token, name = params.split(' :', 1)
                device['name'] = name
                continue
            if action == 'service':
                host, port = params.split(' ', 1)
                device['service'] = (host, int(port))
                continue
            if action == 'connection':
                device['connection'] = params.split(' :', 1)
                continue
            if action == 'highlight-word':
                device['highlight-word'].append(params.lstrip(':'))
                continue
            if action == 'highlight-sound' or 'message-sound':
                device[action] = params.lstrip(':')
                continue
        print 'Registered new push device', device['name']

    def delete(self, params):
        token = params.split(' ', 1)[1].lstrip(':')
        if token in self.devices:
            print 'Unregistered push device', self.devices[token]['name']
            del self.devices[token]

    def update(self):
        if self.sendq.empty(): return

        line = self.sendq.get()
        mask, message = line.split(' ', 1)
        if not message.startswith('PRIVMSG'): return
        action, chan, message = message.split(' ', 2)
        message = message.lstrip(':')

        for token, device in self.devices.items():
            for word in device['highlight-word']:
                if message.find(word) != -1 or re.search(word, message):
                    self.push(device, token, mask, chan, message)

    def push(self, device, token, mask, chan, message):
        sock = socket()
        sock = wrap_socket(sock, server_side=True)
        sock.connect((device['service']))
        sock.write(json.dumps({
            'device-token': token,
            'message': message,
            'sender': mask.split('!', 1)[0],
            'room': chan,
            'server': self.server,
            'badge': 1,
            'sound': device['highlight-sound'],
        }, indent=4) + '\n')
        sleep(0.5)
        sock.close()
        print 'Pushed message to', device['name']

class Relay(object):
    def __init__(self, address, upstream):
        self.address = tuple(address)
        self.upstream = upstream
        self.upstream.relay_line = self.broadcast_line
        self.push = PushServer(upstream.address[0])

        self.sock = socket()
        self.sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        self.sock.bind(self.address)
        self.sock.listen(2)

        self.clients = [self.push]

    def update(self):
        if select([self.sock], [], [], 0.01)[0]:
            sock, address = self.sock.accept()
            client = RelayClient(address, sock, self.upstream)
            client.relay_line = self.upstream.send
            client.close = self.clients.remove
            client.register_push = self.push.register
            client.delete_push = self.push.delete
            self.clients.append(client)

        for client in self.clients:
            client.update()

    def broadcast_line(self, line):
        for client in self.clients:
            client.sendq.put(line + '\n')

def main():
    servers = {}
    relays = {}
    for info in conf('servers'):
        if not info['enabled']: continue
        server = Client(info['address'], info['nick'])
        servers[info['name']] = server
        server.connect()
        print 'Connected to upstream server', info['name']

        relay = Relay(info['relay_address'], server)
        relays[info['name']] = relay

    while True:
        for name in servers:
            servers[name].update()

        for name in relays:
            relays[name].update()

if __name__ == '__main__':
    main()
