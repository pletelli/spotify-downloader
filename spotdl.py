#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
from core import const
from core import handle
from core import metadata
from core import convert
from core import internals
from core import spotify_tools
from core import youtube_tools

from collections import defaultdict
from slugify import slugify
import json
import spotipy
import subprocess
import urllib.request
import os
import sys
import time
import platform
import pprint


def check_exists(music_file, raw_song, meta_tags):
    """ Check if the input song already exists in the given folder. """
    log.debug('Cleaning any temp files and checking '
              'if "{}" already exists'.format(music_file))
    songs = os.listdir(const.args.folder)
    for song in songs:
        if song.endswith('.temp'):
            os.remove(os.path.join(const.args.folder, song))
            continue
        # check if any song with similar name is already present in the given folder
        if song.startswith(music_file):
            log.debug('Found an already existing song: "{}"'.format(song))
            if internals.is_spotify(raw_song):
                # check if the already downloaded song has correct metadata
                # if not, remove it and download again without prompt
                already_tagged = metadata.compare(os.path.join(const.args.folder, song),
                                                  meta_tags)
                log.debug('Checking if it is already tagged correctly? {}',
                                                            already_tagged)
                if not already_tagged:
                    os.remove(os.path.join(const.args.folder, song))
                    return False

            log.warning('"{}" already exists'.format(song))
            if const.args.overwrite == 'prompt':
                log.info('"{}" has already been downloaded. '
                         'Re-download? (y/N): '.format(song))
                prompt = input('> ')
                if prompt.lower() == 'y':
                    os.remove(os.path.join(const.args.folder, song))
                    return False
                else:
                    return True
            elif const.args.overwrite == 'force':
                os.remove(os.path.join(const.args.folder, song))
                log.info('Overwriting "{}"'.format(song))
                return False
            elif const.args.overwrite == 'skip':
                log.info('Skipping "{}"'.format(song))
                return True
    return False


def download_list(text_file):
    """ Download all songs from the list. """
    with open(text_file, 'r') as listed:
        lines = (listed.read()).splitlines()
    # ignore blank lines in text_file (if any)
    try:
        lines.remove('')
    except ValueError:
        pass

    log.info(u'Preparing to download {} songs'.format(len(lines)))
    downloaded_songs = []
    analysed_songs = []

    for number, raw_song in enumerate(lines, 1):
        print('')
        try:
            filepath = download_single(raw_song, number=number)
        # token expires after 1 hour
        except spotipy.client.SpotifyException:
            # refresh token when it expires
            log.debug('Token expired, generating new one and authorizing')
            new_token = spotify_tools.generate_token()
            spotify_tools.spotify = spotipy.Spotify(auth=new_token)
            filepath = download_single(raw_song, number=number)
        # detect network problems
        except (urllib.request.URLError, TypeError, IOError):
            lines.append(raw_song)
            # remove the downloaded song from file
            internals.trim_song(text_file)
            # and append it at the end of file
            with open(text_file, 'a') as myfile:
                myfile.write(raw_song + '\n')
            log.warning('Failed to download song. Will retry after other songs\n')
            # wait 0.5 sec to avoid infinite looping
            time.sleep(0.5)
            continue

        downloaded_songs.append(raw_song)
        analyse_single(filepath)
        analysed_songs.append(raw_song)
        log.debug('Removing downloaded song from text file')
        internals.trim_song(text_file)


    return downloaded_songs


def download_single(raw_song, number=None):
    """ Logic behind downloading a song. """
    if internals.is_youtube(raw_song):
        log.debug('Input song is a YouTube URL')
        content = youtube_tools.go_pafy(raw_song, meta_tags=None)
        raw_song = slugify(content.title).replace('-', ' ')
        meta_tags, audio_analysis, audio_features = spotify_tools.generate_metadata(raw_song)
    else:
        meta_tags, audio_analysis, audio_features = spotify_tools.generate_metadata(raw_song)
        content = youtube_tools.go_pafy(raw_song, meta_tags)

    if content is None:
        log.debug('Found no matching video')
        return

    if const.args.download_only_metadata and meta_tags is None:
        log.info('Found no metadata. Skipping the download')
        return

    # "[number]. [artist] - [song]" if downloading from list
    # otherwise "[artist] - [song]"
    youtube_title = youtube_tools.get_youtube_title(content, number)
    log.info('{} ({})'.format(youtube_title, content.watchv_url))

    # generate file name of the song to download
    songname = content.title

    if meta_tags is not None:
        refined_songname = internals.generate_songname(const.args.file_format, meta_tags)
        log.debug('Refining songname from "{0}" to "{1}"'.format(songname, refined_songname))
        if not refined_songname == ' - ':
            songname = refined_songname
    else:
        log.warning('Could not find metadata')
        songname = internals.sanitize_title(songname)

    if const.args.dry_run:
        return

    if not check_exists(songname, raw_song, meta_tags):
        # deal with file formats containing slashes to non-existent directories
        songpath = os.path.join(const.args.folder, os.path.dirname(songname))
        os.makedirs(songpath, exist_ok=True)
        input_song = songname + const.args.input_ext
        output_song = songname + const.args.output_ext
        if youtube_tools.download_song(input_song, content):
            print('')
            try:
                convert.song(input_song, output_song, const.args.folder,
                             avconv=const.args.avconv)
            except FileNotFoundError:
                encoder = 'avconv' if const.args.avconv else 'ffmpeg'
                log.warning('Could not find {0}, skipping conversion'.format(encoder))
                const.args.output_ext = const.args.input_ext
                output_song = songname + const.args.output_ext

            if not const.args.input_ext == const.args.output_ext:
                os.remove(os.path.join(const.args.folder, input_song))
            if not const.args.no_metadata and meta_tags is not None:
                metadata.embed(os.path.join(const.args.folder, output_song), meta_tags)
            if not const.args.no_file_storage and meta_tags is not None:
                with open(os.path.join(const.args.folder, 'metadata.csv'), 'a+') as metadata_file:
                    spotify_keys = ['id', 'name', 'popularity', 'track_number', 'genre',
                        'release_date', 'publisher', 'total_tracks', 'lyrics', 'year',
                        'duration', 'external_ids.isrc', 'artists.name', 'artists.id',
                        'album.name', 'album.id', 'album.release_date',
                        'album.release_date_precision']
                    meta_to_store = defaultdict(lambda: defaultdict())
                    for key in spotify_keys:
                        if '.' in key:
                            key1, key2 = key.split('.')
                            if 'artists.' not in key:
                                meta_to_store['spotify'][key] = meta_tags[key1][key2]
                            else:
                                meta_to_store['spotify'][key] = meta_tags[key1][0][key2]
                        else:
                            meta_to_store['spotify'][key] = meta_tags[key]
                    meta_to_store['youtube'] = {'videoid': content.videoid,
                                                'title': content.title,
                                                'duration': content.duration}
                    metadata_file.write(json.dumps(meta_to_store))
                    metadata_file.write('\n')
                    print(json.dumps(meta_to_store))
            # return filepath
            return os.path.join(const.args.folder, output_song)

            # TODO:perrine: commenter qu'il n'y a pas de score retournés par l'API search de google
            # TODO:perrine: sauver dans un db

    def analyse_single(filepath):
        """Launch ircam audio analyser""""
        command = ['echo ${const.args.ircam_key} | ./ircam_music_description_demo-1.1.0',
                   '-i', filepath,
                   '-o', self.output_file ]

        log.debug(command)
        return subprocess.call(command)


if __name__ == '__main__':
    const.args = handle.get_arguments()
    internals.filter_path(const.args.folder)

    const.log = const.logzero.setup_logger(formatter=const.formatter,
                                      level=const.args.log_level)
    log = const.log
    log.debug('Python version: {}'.format(sys.version))
    log.debug('Platform: {}'.format(platform.platform()))
    log.debug(pprint.pformat(const.args.__dict__))

    try:
        if const.args.song:
            download_single(raw_song=const.args.song)
        elif const.args.list:
            download_list(text_file=const.args.list)
        elif const.args.playlist:
            spotify_tools.write_playlist(playlist_url=const.args.playlist)
        elif const.args.album:
            spotify_tools.write_album(album_url=const.args.album)
        elif const.args.username:
            spotify_tools.write_user_playlist(username=const.args.username)

        # actually we don't necessarily need this, but yeah...
        # explicit is better than implicit!
        sys.exit(0)

    except KeyboardInterrupt as e:
        log.exception(e)
        sys.exit(3)
