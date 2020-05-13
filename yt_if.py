#!/usr/bin/python3
# -*- coding: utf-8 -*-
import pafy
import json
import os
import re
import sys
import logging

import gi 
gi.require_version('Gst', '1.0')
#gi.require_version('GstPbutils', '1.0')
gi.require_version('GLib', '2.0')
gi.require_version('GObject', '2.0')
from gi.repository import GLib, GObject, Gst #, GstPbutils
Gst.init(sys.argv)

RSP_TITLES = '0'
RSP_TIME = '1'
RSP_END_TRACK = '2'
RSP_STATUS = '3'


# def forward_callback(self, w):
#         rc, pos_int = self.player.query_position(Gst.Format.TIME)
#         seek_ns = pos_int + 10 * 1000000000
#         print 'Forward: %d ns -> %d ns' % (pos_int, seek_ns)
#         self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, seek_ns)

ISO8601_TIMEDUR_EX = re.compile(r'PT((\d{1,3})H)?((\d{1,3})M)?((\d{1,2})S)?')

class VInfo:
    position = 0
    duration = 0
    vid = ""
    title = ""

class YT:
    played = False
    cb = None
    ioloop = None
    stop_thread = False

    # vinfo = {
    #     "position" : 0,
    #     "duration" : 0,
    #     "vid" : "",
    #     "title" : "",
    # }

    vinfo = VInfo()

    @staticmethod
    def set_api_key(key):
        pafy.set_api_key(key)

    @classmethod
    def on_playing_finished(cls):
        cls.played = True
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
    def bus_call(cls, bus, message, loop):
        t = message.type
        if t == Gst.MessageType.EOS:
            #sys.stdout.write("End-of-stream\n")
            cls.on_playing_finished(cls)
            loop.quit()
            if cls.ioloop:
                # Send signal that playing was stopped
                cls.ioloop.add_callback(cls.cb, "2")
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            #sys.stderr.write("Error: %s ::: %s\n" % (err, debug))

            cls.on_playing_finished(cls)
            loop.quit()
        return True

    @classmethod
    def one_second_tick(cls, loop, pipeline):
        if cls.stop_thread:
            logging.info("Stopping inside thread")
            cls.stop_thread = False
            loop.quit()
            if cls.ioloop:
                # Send signal that playing was stopped
                cls.ioloop.add_callback(cls.cb, RSP_END_TRACK)

            return False

        _, cls.vinfo.position = pipeline.query_position(Gst.Format.TIME)
        if cls.vinfo.position == -1:
            return False
        _, cls.vinfo.duration = pipeline.query_duration(Gst.Format.TIME)
        # print("\rPosition: %s of %s" % (Gst.TIME_ARGS(position).split('.',1)[0], 
        #                 Gst.TIME_ARGS(duration).split('.',1)[0]), end ="")
        if cls.ioloop:
            cls.ioloop.add_callback(cls.cb, RSP_TIME + "%d" % int(cls.vinfo.position/1000000000))

        return True

    @classmethod
    def search(cls, yt_title, nextPageToken = None, prevPageToken = None):
        #a=pafy.call_gdata('search', {'q':'Инструментальное кино Цой', 'maxResults': 50, 'part': 'id,snippet'})
        yt_q =  {'q':yt_title, 'maxResults': 5, 'part': 'id'}
        if nextPageToken and nextPageToken != "":
            yt_q["pageToken"] = nextPageToken
        if prevPageToken and prevPageToken != "":
            yt_q["pageToken"] = prevPageToken


        a=pafy.call_gdata('search', yt_q)
        #print(json.dumps(a, indent=4))

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

        #    title=snippet.get('localized', {'title':snippet.get('title',
        #                                                 '[!!!]')}).get('title', '[!]'),

            title = snippet.get('title', '').strip()
            result.append({"id" : vid, "title" : title, "time": duration})

        return {"nextPageToken" : nextPageToken, "prevPageToken" : prevPageToken, "pl" : result}

    @classmethod
    def play(cls, vid, ioloop, cb):
        cls.played = False
        cls.cb = cb
        cls.ioloop = ioloop

        count = 0
        while not YT.played and count < 5:
            passed = False
            pcnt = 0
            while not passed and pcnt < 5:
                try:
                    video = pafy.new('youtube.com/watch?v=' + vid[4:])
                    passed = True
                except:
                    pcnt +=1

            s = video.getbestaudio()
            #print(s.bitrate, s.extension)

            # Gst.Pipeline
            pipeline = Gst.parse_launch('uridecodebin uri="' + s.url + '" ! audioconvert ! audioresample ! ' \
            'audio/x-raw,rate=48000,channels=2 ! audioconvert ! rtpL16pay ! queue ! udpsink clients=127.0.0.1:10001')

            bus = pipeline.get_bus()
            bus.add_signal_watch()

            cls.on_playing_started(vid, video.title)
            pipeline.set_state(Gst.State.PLAYING)

            loop = GLib.MainLoop()
            GLib.timeout_add_seconds(1, YT.one_second_tick, loop, pipeline)
            bus.connect ("message", YT.bus_call, loop)
            loop.run()
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_state(Gst.CLOCK_TIME_NONE)

            cls.on_playing_finished()

                


