class GlobalVariables:
    RSP_TITLES = '0'
    RSP_TIME = '1'
    RSP_END_TRACK = '2'
    RSP_COMMENT = '3'
    RSP_PLAYLIST = '4'
    RSP_PLAY = '5'
    RSP_STOP = '6'
    RSP_PLLIST = '7'

    playList = None
    stopReceived = False
    nowPlayingIdx = -1
    nowPlayingVid = -1

    maxSearchResults = 5
    playlistFolder = "./playlists"
    domain = ""
    logfile = "ytplay.log"
    audioOut = ""

    pid_file = ""