#!/usr/bin/python3
# -*- coding: utf-8 -*-
import pafy
import json
import os
import re
import sys
import logging
import threading

import gi 
gi.require_version('Gst', '1.0')
#gi.require_version('GstPbutils', '1.0')
gi.require_version('GLib', '2.0')
gi.require_version('GObject', '2.0')
from gi.repository import GLib, GObject, Gst #, GstPbutils
Gst.init(sys.argv)

from globalvars import GlobalVariables as g

ISO8601_TIMEDUR_EX = re.compile(r'PT((\d{1,3})H)?((\d{1,3})M)?((\d{1,2})S)?')

class VInfo:
    position = 0
    duration = 0
    vid = ""
    title = ""

# Redefine MainLoop to get rid of system signal reassigment
class PlayerMainLoop(GLib.MainLoop):
    # Backwards compatible constructor API
    def __new__(cls, context=None):
        return GLib.MainLoop.new(context, False)

    # Retain classic pygobject behaviour of quitting main loops on SIGINT
    def __init__(self, context=None):
        pass

    def __del__(self):
        pass

class YT:
    played = False
    cb = None
    ioloop = None

    loop = None
    pipeline = None
    forceStop = False

    lock = None

    vinfo = VInfo()

    @staticmethod
    def set_api_key(key):
        pafy.set_api_key(key)

    @classmethod
    def on_playing_finished(cls):
        cls.vinfo.vid = ""
        cls.vinfo.title = ""
        cls.vinfo.position = 0
        cls.vinfo.duration = 0

    @classmethod
    def on_playing_started(cls, vid, title):
        cls.played = False
        cls.vinfo.vid = vid
        cls.vinfo.title = title
        cls.vinfo.position = 0
        cls.vinfo.duration = 0

    @classmethod
    def fforward(cls, time):
        rc, pos_int = cls.pipeline.query_position(Gst.Format.TIME)
        seek_ns = pos_int + time * 1000000000
        logging.debug('Forward: %d ns -> %d ns' % (pos_int, seek_ns))
        cls.lock.acquire()
        cls.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, seek_ns)
        cls.lock.release()

    @classmethod
    def seek(cls, percent):
        rc, duration = cls.pipeline.query_duration(Gst.Format.TIME)
        seek_ns = duration * percent
        logging.debug('Seek -> %d ns' % seek_ns)
        cls.lock.acquire()
        cls.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, seek_ns)
        cls.lock.release()

    @classmethod
    def bus_call(cls, bus, message, loop):
        if cls.forceStop:
            loop.quit()
            cls.played = True
        t = message.type
        if t == Gst.MessageType.EOS:
            # sys.stdout.write("End-of-stream\n")
            cls.played = True
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            # err, debug = message.parse_error()
            # sys.stderr.write("Error: %s ::: %s\n" % (err, debug))
            cls.on_playing_finished()
            loop.quit()
        return True

    @classmethod
    def one_second_tick(cls, loop, pipeline):
        cls.lock.acquire()
        if cls.forceStop:
            loop.quit()
            cls.played = True
        if not loop.is_running():
            cls.lock.release()
            return False
        _, cls.vinfo.position = pipeline.query_position(Gst.Format.TIME)
        if cls.vinfo.position == -1:
            cls.lock.release()
            return True
        _, cls.vinfo.duration = pipeline.query_duration(Gst.Format.TIME)
        # print("\rPosition: %s of %s" % (Gst.TIME_ARGS(position).split('.',1)[0], 
        #                 Gst.TIME_ARGS(duration).split('.',1)[0]), end ="")
        if cls.ioloop:
            cls.ioloop.add_callback(cls.cb, g.RSP_TIME + "%d %d" % (int(cls.vinfo.position/1000000000),
                                                                    int(cls.vinfo.duration/1000000000)))
        cls.lock.release()
        return True

    @classmethod
    def search(cls, yt_title, nextPageToken = None, prevPageToken = None):
        yt_q =  {'q':yt_title, 'maxResults': g.maxSearchResults, 'part': 'id'}
        if nextPageToken and nextPageToken != "":
            yt_q["pageToken"] = nextPageToken
        if prevPageToken and prevPageToken != "":
            yt_q["pageToken"] = prevPageToken

        a=pafy.call_gdata('search', yt_q)

        id_list = []
        pl_list = []
        result = []

        nextPageToken = a.get("nextPageToken", "")
        prevPageToken = a.get("prevPageToken", "");


        for c in a["items"]:
            if "playlistId" in c["id"]:
                # got playlist
                pl_list.append(c["id"]["playlistId"])
            elif "videoId" in c["id"]:
                id_list.append(c["id"]["videoId"])

        #qs = {'part':'contentDetails,statistics,snippet',
        qs = {'part':'contentDetails,snippet',
            'id': ','.join(id_list),
            #'fields' : 'items(id,snippet(title,description,thumbinails/default,localized(title,description)),contentDetails/duration)'
            #'fields' : 'items(id,snippet(title,thumbnails/default,localized(title)),contentDetails/duration)'
            'fields' : 'items(id,snippet(title),contentDetails/duration)'
        }

        wdata = pafy.call_gdata('videos', qs)
        items = wdata.get('items', [])

        for item in items:
            duration = item.get('contentDetails', {}).get('duration')
            vid = item.get('id')
            snippet = item.get('snippet', {})

            if duration:
                duration = ISO8601_TIMEDUR_EX.findall(duration)
                if len(duration) > 0:
                    _, hours, _, minutes, _, seconds = duration[0]
                    duration = [seconds, minutes, hours]
                    duration = [int(v) if len(v) > 0 else 0 for v in duration]
                    duration = sum([60**p*v for p, v in enumerate(duration)])
                else:
                    duration = 30
            else:
                duration = 30

            # title=snippet.get('localized', {'title':snippet.get('title',
            #                                   '[!!!]')}).get('title', '[!]'),

            title = snippet.get('title', '').strip()
            result.append({"id" : vid, "title" : title, "time": duration})

        return {"nextPageToken" : nextPageToken, "prevPageToken" : prevPageToken, "pl" : result}

    @classmethod
    def play(cls, vid, ioloop, cb):
        cls.played = False
        cls.cb = cb
        cls.ioloop = ioloop

        if cls.lock is None:
            cls.lock = threading.Lock()

        for cnt in range(3):
            video = None
            for pcnt in range(3):
                try:
                    logging.debug("Getting information about %s" % vid[4:])
                    video = pafy.new('youtube.com/watch?v=' + vid[4:])
                    break
                except:
                    logging.error("Error getting %s data" % vid[4:])
                    pass

            if video is None:
                continue

            s = video.getbestaudio()

            # Gst.Pipeline
            logging.debug("Creating pipeline")
            try:
                cls.pipeline = Gst.parse_launch('uridecodebin uri="' + s.url + '" ! ' + g.audioOut)
            except GLib.Error as err:
                logging.error("Gst.parse_launch error: %s. Parsed str: '%s'" % (err, g.audioOut))
                g.stopReceived = True
                break

            bus = cls.pipeline.get_bus()
            bus.add_signal_watch()

            cls.on_playing_started(vid, video.title)
            cls.pipeline.set_state(Gst.State.PLAYING)

            logging.info("Playing %s - %s" %(vid[4:], video.title))

            cls.loop = PlayerMainLoop()
            GLib.timeout_add_seconds(1, YT.one_second_tick, cls.loop, cls.pipeline)
            bus.connect ("message", YT.bus_call, cls.loop)
            try:
                logging.debug("Staring play loop")
                cls.loop.run()
            except KeyboardInterrupt:
                cls.played = True

            logging.debug("Play loop finished")
            cls.pipeline.set_state(Gst.State.NULL)
            cls.pipeline.get_state(Gst.CLOCK_TIME_NONE)

            if cls.played or cls.forceStop:
                cls.loop = None
                break

            cls.loop = None
        
        cls.played = True
        cls.loop = None
        cls.on_playing_finished()
                


