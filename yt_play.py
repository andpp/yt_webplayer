#!/usr/bin/python3
# -*- coding: utf-8 -*-

import os
import logging
import re
import uuid
import traceback
import threading
import configparser
import urllib
import signal
import sys
import time
import pwd
import grp

from pathvalidate import sanitize_filepath
from logging.handlers import RotatingFileHandler, WatchedFileHandler

import tornado.escape
import tornado.ioloop
import tornado.web
import tornado.websocket
import tornado.locks
import tornado.gen
# import tornado.log
from tornado.options import define, options, parse_command_line

import youtube_dl

from yt_if import YT
from globalvars import GlobalVariables as g

class PlDownloader(threading.Thread):
    def __init__(self, pl, ioloop, cb):
        ''' Constructor. '''
        threading.Thread.__init__(self)
        self.ydl_opts = {
            'ignoreerrors': True,
            # 'quiet': True
            'logger': self,
            'no_color': True,
            'cachedir': '/tmp'
        }
        self.ioloop = ioloop
        self.cb = cb
        self.plname = pl

    def debug(self, m):
        self.notify("%s" % m)

    def error(self, m):
        self.notify("%s" % m)

    def warning(self, m):
        self.notify("%s" % m)

    def notify(self, m):
        self.ioloop.add_callback(self.cb, g.RSP_COMMENT + m)

    def  sec_to_time(self, s):
        h = '{0:02d}'.format(int(s / (60 * 60)))
        m = '{0:02d}'.format(int((s % (60 * 60)) / 60))
        s = '{0:02d}'.format(s % 60)
        return h + ":" + m + ":" + s

    def run(self):
        playlist = "https://www.youtube.com/playlist?list=" + self.plname

        with youtube_dl.YoutubeDL(self.ydl_opts) as ydl:
            playlist_dict = ydl.extract_info(playlist, download=False)

            if playlist_dict is None:
                return

            # print(playlist_dict['id'], playlist_dict['title'])
            name = sanitize_filepath(os.path.normpath(playlist_dict['title']))
            fname = os.path.join(g.playlistFolder,os.path.basename(name))
            while(os.path.exists(fname)):
                fname = fname + '_'

            with open(fname,"w") as f:
                for video in playlist_dict['entries']:
                    if not video:
                        # logging.debug('Unable to get info about video in playlist')
                        continue
                    f.write("%s %s - %s\n" % (video.get('id'), self.sec_to_time(video.get('duration')),  video.get('title')) )

lock = tornado.locks.Lock()

def createDaemon():
    try:
        if os.getuid() == 0:
            g.pid_file = "/var/run/ytplay/ytplay.pid"
            pid_dir = os.path.dirname(g.pid_file)
            if not os.path.exists(pid_dir):
                os.mkdir(pid_dir)
        else:
            g.pid_file = "/tmp/ytplay.pid"
        pid = os.fork()
    except OSError as e:
        raise Exception("%s [%d]" % (e.strerror, e.errno))

    if pid == 0:
        os.setsid()
        # Put to sleep so parent will be able to create pid file
        time.sleep(0.5)
    else:
        f=open(g.pid_file,"w")
        f.write(str(pid) + "\n")
        f.close()
        os._exit(0)

def drop_privileges(uid_name='nobody', gid_name='nogroup'):
    """ Drop privileges of current process to given user and group """
    
    if os.getuid() != 0:
        # We're not root
        return

    logging.info("Switching privileges to %s:%s" % (uid_name, gid_name))
    # Get the uid/gid from the name
    running_uid = pwd.getpwnam(uid_name).pw_uid
    running_gid = grp.getgrnam(gid_name).gr_gid

    # Change ownership of the pid file in order to be able
    # to remove it after exiting
    os.chown(os.path.dirname(g.pid_file), running_uid, running_gid) 
    os.chown(g.pid_file, running_uid, running_gid)
    os.chown(g.logfile, running_uid, running_gid)

    # Remove group privileges
    os.setgroups([])

    # Try setting the new uid/gid
    os.setgid(running_gid)
    os.setuid(running_uid)

    # Ensure a very conservative umask
    old_umask = os.umask(0o77) 

class MyBaseHandler(tornado.web.RequestHandler):
    def write_error(self, status_code, **kwargs):
        if self.settings.get("serve_traceback") and "exc_info" in kwargs:
            # in debug mode, try to send a traceback
            self.set_header('Content-Type', 'text/plain')
            for line in traceback.format_exception(*kwargs["exc_info"]):
                self.write(line)
            self.finish()
        else:
            self.set_status(status_code)
            if kwargs['reason']:
                self.finish(kwargs['reason'])
            else: 
                self.finish("<html><title>%(code)d: %(message)s</title>"
                    "<body>%(code)d: %(message)s</body></html>" % {
                        "code": status_code,
                        "message": self._reason,
                    })

def get_num_by_vid(vid):
    if g.playList:
        for i, e in enumerate(g.playList):
            if(vid == e['id']):
                return i
    return -1

def get_active():
    if g.playList:
        for i, e in enumerate(g.playList):
            if(e['active']):
                return i
    return -1

def deactivate_all():
    if g.playList:
        for i, e in enumerate(g.playList):
            e['active'] = False

def send_playlist(ioloop, cb):
    ioloop.add_callback(cb, g.RSP_PLAYLIST + tornado.escape.json_encode({"uuid" : "", "pl" : g.playList}))

def yt_play_thread(vid, ioloop, cb):
    active = get_num_by_vid(vid)
    if(active == -1):
            return
    while(not g.stopReceived):
        g.nowPlayingIdx = active
        deactivate_all()
        g.playList[active]['active'] = True
        vid = g.playList[active]['id']
        last_active = active  # Save last active play
        send_playlist(ioloop, cb)
        logging.info("Starting to play %s" % vid)
        YT.play(vid, ioloop, cb)
        active = get_active()
        deactivate_all()
        if(active != -1):  
            active +=1
        else:
            # Active line was removed. Use previous position
            active = last_active
        if len(g.playList) <= active:
            break  # Stop if reached enf of playlist

    # Send signal that playing was stopped
    ioloop.add_callback(cb, g.RSP_END_TRACK)
    # Send updated playlist
    send_playlist(ioloop, cb)
    YTSocketHandler.play_thread = None



class MainHandler(MyBaseHandler):
    def get(self):
        self.render(os.path.join(g.html_path, "index.html"))

class YTSocketHandler(tornado.websocket.WebSocketHandler):
    waiters = set()
    play_thread = None

    def __init__(self, application, request, **kwargs):
        tornado.websocket.WebSocketHandler.__init__(self, application, request, **kwargs)

    def get_compression_options(self):
        # Non-None enables compression with default options.
        return {}

    def open(self):
        self.remote_ip = self.request.headers.get("X-Real-IP") or \
            self.request.headers.get("X-Forwarded-For") or \
            self.request.remote_ip
        logging.info("Open connection from %s " % self.remote_ip)
        YTSocketHandler.waiters.add(self)

    def on_close(self):
        logging.info("Close connection from %s" % self.remote_ip)
        YTSocketHandler.waiters.remove(self)

    @classmethod
    def send_updates(cls, msg):
        logging.debug("sending message to %d waiters: %s", len(cls.waiters), msg)
        for waiter in cls.waiters:
            try:
                waiter.write_message(msg)
            except:
                logging.error("Error sending message", exc_info=True)
    
    @tornado.gen.coroutine
    def yt_search(self, title, nextpage):
        logging.info("Starting search for %s" % title)
        res = YT.search(title, nextpage)
        logging.debug(res)
        YTSocketHandler.send_updates(g.RSP_TITLES + tornado.escape.json_encode(res))

    @classmethod
    @tornado.gen.coroutine
    def yt_stop(cls):
        if not cls.play_thread is None:
            logging.debug("Stopping player")
            YT.played = True
            YT.forceStop = True
            g.stopReceived = True
            if YT.loop:
                YT.loop.quit()
            cls.play_thread.join()
            logging.debug("Player stopped")
            # cls.send_updates(g.RSP_END_TRACK)

    @classmethod
    @tornado.gen.coroutine
    def yt_play(cls, vid):
        if not cls.play_thread is None:
            cls.yt_stop()

        YT.forceStop = False
        ioloop = tornado.ioloop.IOLoop.instance()
        cls.play_thread = threading.Thread(target=yt_play_thread, args=(vid, ioloop, cls.send_updates))
        g.stopReceived = False
        cls.play_thread.start()
        cls.send_updates('3'+ tornado.escape.json_encode({"vid" : vid}))

    @tornado.gen.coroutine
    def save_playlist(self, name):
        name = sanitize_filepath(os.path.normpath(name))
        fname = os.path.join(g.playlistFolder,os.path.basename(name))
        logging.info("Saving playlist '%s'" % fname)
        with open(fname,'w') as f:
            for e in g.playList:
                f.write("%s %s\n" % (e['id'][4:] , e['txt']))

    @tornado.gen.coroutine
    def load_playlist(self, name):
        name = sanitize_filepath(os.path.normpath(name))
        fname = os.path.join(g.playlistFolder,os.path.basename(name))
        logging.info("Loading playlist '%s'" % fname)
        with open(fname,'r') as f:
            content = f.read().splitlines()
        g.playList = []
        i = 0
        for e in content:
            try:
                [vid, txt] = e.split(" ", 1)
                g.playList.append({'id' : '{0:04d}'.format(i) + vid, 'txt' : txt, 'active': False})
                i += 1
            except:
                pass
        YTSocketHandler.send_updates(g.RSP_PLAYLIST + tornado.escape.json_encode({"uuid" : "", "pl" : g.playList}))

    @tornado.gen.coroutine
    def list_playlists(self):
        files = [f for f in os.listdir(g.playlistFolder) if os.path.isfile(os.path.join(g.playlistFolder, f))]
        files.sort()
        YTSocketHandler.send_updates(g.RSP_PLLIST + tornado.escape.json_encode({"pl" : files}))

    @tornado.gen.coroutine
    def delete_playlist(self, name):
        name = sanitize_filepath(os.path.normpath(name))
        logging.info("Deleting playlist '%s'" % name)

        try:
            name = os.path.basename(name)
            fname = os.path.join(g.playlistFolder, name)
            if os.path.isfile(fname):
                os.remove(fname)
        except:
            pass
        self.list_playlists()

    @tornado.gen.coroutine
    def rename_playlist(self, oname, nname):
        oname = sanitize_filepath(os.path.normpath(oname))
        nname = sanitize_filepath(os.path.normpath(nname))
        logging.info("Renaming playlist '%s' to '%s'" % (oname, nname))

        try:
            oname = os.path.basename(oname)
            nname = os.path.basename(nname)
            ofname = os.path.join(g.playlistFolder, oname)
            nfname = os.path.join(g.playlistFolder, nname)
            if os.path.isfile(ofname):
                os.rename(ofname, nfname)
        except:
            pass
        self.list_playlists()

    @classmethod
    @tornado.gen.coroutine
    def get_pl_from_yt(cls, plname):
        ioloop = tornado.ioloop.IOLoop.instance()
        args = plname.split('?')
        if len(args) > 1:
            res = urllib.parse.parse_qs(args[1])
            if 'list' in res:
                plname = res['list'][0]
        logging.info("Download playlist from YouTube: %s" % plname)
        a =  PlDownloader(plname, ioloop, cls.send_updates)
        a.start()

    @tornado.gen.coroutine
    def fforward(self, time):
        YT.fforward(time)

    def check_origin(self, origin: str) -> bool:
        parsed_origin = urllib.parse.urlparse(origin)
        origin = parsed_origin.netloc
        origin = origin.lower()

        if g.domain != "":
            host = g.domain
        else:
            host = self.request.headers.get("Host")

        # Check to see that origin matches host directly, including ports
        return origin == host

    def on_message(self, message):
        logging.debug("got message %r", message)
        try:
            if isinstance(message, type(b'')):
                return
            parsed = tornado.escape.json_decode(message)
            logging.debug(parsed)
            cmd = parsed.get("cmd")
            if cmd == 'search':
                if "title" in parsed:
                    logging.debug("Going to search")
                    np = parsed.get("pagetoken","")
                    if np != "" : logging.debug("Search for next page")
                    self.yt_search(parsed["title"], np )

            elif cmd == 'play':
                if "id" in parsed:
                    self.yt_play(parsed["id"])

            elif cmd == 'stop':
                g.stopReceived = True
                YTSocketHandler.send_updates(g.RSP_STOP + tornado.escape.json_encode({"vid" : parsed.get("vid", "_")}))
                self.yt_stop()

            elif cmd == 'updatePlaylist':
                if "pl" in parsed:
                    g.playList = parsed['pl']
                    YTSocketHandler.send_updates(g.RSP_PLAYLIST + tornado.escape.json_encode({"uuid" : parsed.get("uuid", ""), "pl" : g.playList}))

            elif cmd == 'getPlaylist':
                self.write_message(g.RSP_PLAYLIST + tornado.escape.json_encode({"uuid" : "", "pl" : g.playList}))

            elif cmd == 'savePlaylist':
                if 'title' in parsed:
                    self.save_playlist(parsed['title'])

            elif cmd == 'loadPlaylist':
                if 'title' in parsed:
                    self.load_playlist(parsed['title'])

            elif cmd == 'listPLaylists':
                self.list_playlists()

            elif cmd == 'delPlaylist':
                if 'title' in parsed:
                    self.delete_playlist(parsed['title'])

            elif cmd == 'renPlaylist':
                if 'otitle' in parsed and 'ntitle' in parsed:
                    self.rename_playlist(parsed['otitle'],parsed['ntitle'])

            elif cmd == 'fforward':
                if 'time' in parsed:
                    self.fforward(parsed['time'])

            elif cmd == 'getPlFromYT':
                if 'title' in parsed:
                    self.get_pl_from_yt(parsed['title'])
                
        except:
            logging.error("error parsing message %s" % message)
            pass

def setup_logging(logfile):
    logging.info("Logging to %s" % g.logfile)
    root_logger = logging.getLogger()

    #handler = RotatingFileHandler(logfile, maxBytes=1024*1024*1024, backupCount=3)
    handler = WatchedFileHandler(logfile,"a+")

    # Update default handler
    root_logger.handlers[0] = handler
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:  %(message)s"))

def main():
    # define("port", default=9180, help="run on the given port", type=int)
    define("debug", default=False, help="run in debug mode")
    define("config", default='/etc/ytplay/ytplay.cfg', help="configuration file")
    parse_command_line()

    config = configparser.ConfigParser()
    if len(config.read(options.config)) > 0:

        if 'main' in config:
            if 'debug' in config['main']:
                options.debug = config['main']['debug'].lower() == 'true'

            g.maxSearchResults = config['main'].get('max_search_results', g.maxSearchResults)
            g.playlistFolder = config['main'].get('playlist_folder', g.playlistFolder)
            g.domain = config['main'].get('domain', '')
            g.logfile = config['main'].get('logfile', g.logfile)
            is_daemon = config['main'].get('daemon', "false").lower()
            user = config['main'].get('user')
            group = config['main'].get('group', 'nogroup')
            port = config['main'].get('port', '9180')
            iface = config['main'].get('interface','')
            

    if not os.path.isdir(g.playlistFolder):
        logging.error("No playlist folder '%s'" % g.playlistFolder)
        exit(1)

    g.html_path = os.path.join(os.path.dirname(__file__), "html")

    settings = dict(
            cookie_secret="7iMKtRBF8VYcjJ0YW3oUCdKs",
            template_path=os.path.join(g.html_path, "static"),
            static_path=os.path.join(g.html_path, "static"),
            xsrf_cookies=True,
            debug=options.debug,
            serve_traceback=False
    )

    handlers = [
            (r"/", MainHandler),
            (r"/ws", YTSocketHandler),
            (r"/js/(.*)", tornado.web.StaticFileHandler, {"path": settings["static_path"]}),
            (r"/APM/(.*)", tornado.web.StaticFileHandler, {"path": os.path.join(settings["static_path"],"APM")})
    ]

    if iface == '':
        iface_name = '*'
    else:
        iface_name = iface
    logging.info('Starting server on %s:%s' % (iface_name, port))
    app = tornado.web.Application(handlers, **settings)
    try:
        app.listen(port = port, address = iface)
    except OSError as e:
        logging.error("Can't bind to port %s. Exitting..." % port)
        sys.exit(1)

    try:
        os.chmod(g.logfile, 0o644)
    except:
        pass

    if is_daemon == 'true':
        createDaemon()
        setup_logging(g.logfile)
        logging.info('Starting server on %s:%s' % (iface_name, port))
        if not (user is None):
            drop_privileges(user, group)


    if 'main' in config:
        if 'api_key' in config['main']:
            logging.info("Found API key. Iniitalizing...")
            YT.set_api_key(config['main']['api_key'])
        if 'audio_out' in config['main']:
            g.audioOut = config['main'].get("audio_out")
        else:
            g.audioOut = "audioconvert ! audioresample ! autoaudiosink"
            logging.info("Using default audio_out: %s" % g.audioOut)


    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGTERM, sigint_handler)
    tornado.ioloop.IOLoop.instance().start()


##########################################################################################################
#    Ctrl-C handler
##########################################################################################################

def sigint_handler(signal, frame):
    logging.info("Exiting...")
    logging.getLogger().handlers[0].close()
    os.remove(g.pid_file)
    os._exit(0)


if __name__ == "__main__":
    main()
