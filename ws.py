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

from pathvalidate import sanitize_filepath


import tornado.escape
import tornado.ioloop
import tornado.web
import tornado.websocket
import tornado.locks
import tornado.gen
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
            'logger': self
        }
        self.ioloop = ioloop
        self.cb = cb
        self.plname = pl

    def debug(self, m):
        self.notify("D %s" % m)

    def error(self, m):
        self.notify("E %s" % m)

    def warning(self, m):
        self.notify("W %s" % m)

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

            # print(playlist_dict['id'], playlist_dict['title'])
            name = sanitize_filepath(os.path.normpath(playlist_dict['title']))
            fname = os.path.join(g.playlistFolder,os.path.basename(name))
            while(os.path.exists(fname)):
                fname = fname + '_'

            with open(fname,"w") as f:
                for video in playlist_dict['entries']:
                    if not video:
                        print('ERROR: Unable to get info. Continuing...')
                        continue
                    f.write("%s %s - %s\n" % (video.get('id'), self.sec_to_time(video.get('duration')),  video.get('title')) )

lock = tornado.locks.Lock()

def createDaemon():
    try:
        pid = os.fork()
    except OSError as e:
        raise Exception("%s [%d]" % (e.strerror, e.errno))

    if pid == 0:
        os.setsid()

        try:
            sys.stdin.close()
            sys.stdout.close()
            sys.stderr.close()
            try:
                logFile = open(g.logfile,"a+",0)
            except:
                logging.error("Error opening logfile %s" % g.logfile)
            sys.stdout = logFile
            sys.stderr = logFile
        except Exception as e:
            logging.error(e)
            pass
    else:
        if os.getuid() == 0:
            f=open("/var/run/ytplay.pid","w")
        else:
            f=open("/tmp/ytplay.pid","w")
        f.write(str(pid) + "\n")
        f.close()
        os._exit(0)

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
        if(len(g.playList) <= active):
            break  # Stop if reached enf of playlist

    # Send signal that playing was stopped
    ioloop.add_callback(cb, g.RSP_END_TRACK)
    # Send updated playlist
    send_playlist(ioloop, cb)



    YTSocketHandler.play_thread = None

class MainHandler(MyBaseHandler):
    def get(self):
        self.render(os.path.join(os.path.dirname(__file__), "index.html"))

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
        logging.debug("Starting search for %s" % title)
        res = YT.search(title, nextpage)
        logging.info(res)
        YTSocketHandler.send_updates(g.RSP_TITLES + tornado.escape.json_encode(res))

    @classmethod
    @tornado.gen.coroutine
    def yt_stop(cls):
        if not cls.play_thread is None:
            logging.debug("Stopping player")
            YT.played = True
            YT.forceStop = True
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
        cls.play_thread.start()
        cls.send_updates('3'+ tornado.escape.json_encode({"vid" : vid}))

    @tornado.gen.coroutine
    def save_playlist(self, name):
        name = sanitize_filepath(os.path.normpath(name))
        fname = os.path.join(g.playlistFolder,os.path.basename(name))
        with open(fname,'w') as f:
            for e in g.playList:
                f.write("%s %s\n" % (e['id'][4:] , e['txt']))

    @tornado.gen.coroutine
    def load_playlist(self, name):
        name = sanitize_filepath(os.path.normpath(name))
        fname = os.path.join(g.playlistFolder,os.path.basename(name))
        with open(fname,'r') as f:
            content = f.read().splitlines()
        g.playList = []
        i = 0
        for e in content:
            try:
                [vid, txt] = e.split(" ", 1)
                g.playList.append({'id' : '{0:04d}'.format(i) + vid, 'txt' : txt})
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
                    g.stopReceived = False
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

def main():
    # define("port", default=9180, help="run on the given port", type=int)
    define("debug", default=False, help="run in debug mode")
    define("config", default='/etc/ytplay/ytplay.cfg', help="configuration file")

    parse_command_line()

    config = configparser.ConfigParser()
    if len(config.read(options.config)) > 0:

        if 'main' in config:
            if 'api_key' in config['main']:
                logging.info("Found API key. Iniitalizing...")
                YT.set_api_key(config['main']['api_key'])

            if 'debug' in config['main']:
                options.debug = config['main']['debug'].lower() == 'true'

            g.maxSearchResults = config['main'].get('max_search_results', g.maxSearchResults)
            g.playlistFolder = config['main'].get('playlist_folder', g.playlistFolder)
            g.domain = config['main'].get('domain', '')
            is_daemon = config['main'].get('daemon', "false").lower()
            port = config['main'].get('port', '9180')
            iface = config['main'].get('interface','')

            if 'audio_out' in config['main']:
                g.audioOut = config['main'].get("audio_out")
            else:
                g.audioOut = "audioconvert ! audioresample ! autoaudiosink"
                logging.info("Using default audio_out: %s" % g.audioOut)
                

    if not os.path.isdir(g.playlistFolder):
        logging.error("No playlist folder '%s'" % g.playlistFolder)
        exit(1)

    if is_daemon == 'true':
        createDaemon()

    settings = dict(
            cookie_secret="7iMKtRBF8VYcjJ0YW3oUCdKs",
            template_path=os.path.join(os.path.dirname(__file__), "static"),
            static_path=os.path.join(os.path.dirname(__file__), "static"),
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
    app.listen(port = port, address = iface)
    signal.signal(signal.SIGINT, sigint_handler)
    tornado.ioloop.IOLoop.instance().start()


##########################################################################################################
#    Ctrl-C handler
##########################################################################################################

def sigint_handler(signal, frame):
        try:
            print('You pressed Ctrl+C!')
        except:
            pass
        finally: 
            os._exit(0)
            #os.kill(os.getpid(), signal.SIGKILL)



if __name__ == "__main__":
    main()