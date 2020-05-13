#!/usr/bin/python3
# -*- coding: utf-8 -*-

import os.path
import logging
import re
import csv
import uuid
import traceback
# import json
import threading
import configparser

import tornado.escape
import tornado.ioloop
import tornado.web
import tornado.websocket
import tornado.locks
import tornado.gen

from tornado.options import define, options, parse_command_line

from yt_if import YT

lock = tornado.locks.Lock()

class GlobalVariables:
    RSP_TITLES = '0'
    RSP_TIME = '1'
    RSP_END_TRACK = '2'
    RSP_STATUS = '3'
    RSP_PLAYLIST = '4'
    RSP_PLAY = '5'
    RSP_STOP = '6'

    playList = None

g = GlobalVariables()

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

def yt_play_thread(vid, ioloop, cb):
    YT.play(vid, ioloop, cb)
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
        logging.info("Open connection")
        YTSocketHandler.waiters.add(self)

    def on_close(self):
        logging.info("Close connection")
        YTSocketHandler.waiters.remove(self)

    @classmethod
    def send_updates(cls, msg):
        logging.info("sending message to %d waiters: %s", len(cls.waiters), msg)
        for waiter in cls.waiters:
            try:
                waiter.write_message(msg)
            except:
                logging.error("Error sending message", exc_info=True)
    
    @tornado.gen.coroutine
    def yt_search(self, title, nextpage):
        logging.info("Starting search for %s" % title)
        res = YT.search(title, nextpage)
        logging.info(res)
        YTSocketHandler.send_updates(g.RSP_TITLES + tornado.escape.json_encode(res))

    @classmethod
    @tornado.gen.coroutine
    def yt_stop(cls):
        if not cls.play_thread is None:
            logging.info("Stopping player")
            YT.stop_thread = True
            cls.play_thread.join()
            logging.info("Player stopped")

    @classmethod
    @tornado.gen.coroutine
    def yt_play(cls, vid):
        if not cls.play_thread is None:
            cls.yt_stop()

        logging.info("Playing %s" % vid)
        ioloop = tornado.ioloop.IOLoop.instance()
        cls.play_thread = threading.Thread(target=yt_play_thread, args=(vid, ioloop, cls.send_updates))
        cls.play_thread.start()
        cls.send_updates('3'+ tornado.escape.json_encode({"vid" : vid}))

    def on_message(self, message):
        logging.info("got message %r", message)
        try:
            if isinstance(message, type(b'')):
                return
            parsed = tornado.escape.json_decode(message)
            logging.info(parsed)
            cmd = parsed.get("cmd")
            if cmd == 'search':
                if "title" in parsed:
                    logging.info("going to search")
                    np = parsed.get("pagetoken","")
                    if np != "" : logging.info("search next page")
                    self.yt_search(parsed["title"], np )
            elif cmd == 'play':
                if "id" in parsed:
                    self.yt_play(parsed["id"])
                    res = {
                        "vid" : parsed["id"]
                    }
                    YTSocketHandler.send_updates(g.RSP_STATUS + tornado.escape.json_encode(res))
            elif cmd == 'stop':
                YTSocketHandler.send_updates(g.RSP_STOP + tornado.escape.json_encode({"vid" : parsed.get("vid", "_")}))
                self.yt_stop()

            elif cmd == 'getStatus':
                res = {
                    "vid"      : YT.vinfo.vid
                }
                self.write_message(g.RSP_STATUS + tornado.escape.json_encode(res))
            elif cmd == 'updatePlaylist':
                if "pl" in parsed:
                    g.playList = parsed['pl']
                    YTSocketHandler.send_updates(g.RSP_PLAYLIST + tornado.escape.json_encode({"uuid" : parsed.get("uuid", ""), "pl" : g.playList}))
            elif cmd == 'getPlaylist':
                    self.write_message(g.RSP_PLAYLIST + tornado.escape.json_encode({"uuid" : "", "pl" : g.playList}))
                
        except:
            logging.log("error parsing message")
            pass

def main():
    define("port", default=9180, help="run on the given port", type=int)
    define("debug", default=True, help="run in debug mode")
    define("config", default='/etc/ytplay/ytplay.cfg', help="configuration file")

    parse_command_line()
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
            (r"/js/(.*)", tornado.web.StaticFileHandler, {"path": settings["static_path"]})
    ]


    config = configparser.ConfigParser()
    if len(config.read(options.config)) > 0:
        
        if 'main' in config:
            if 'api_key' in config['main']:
                logging.info("Found API key. Iniitalizing...")
                YT.set_api_key(config['main']['api_key'])
    

    app = tornado.web.Application(handlers, **settings)
    app.listen(options.port)
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()