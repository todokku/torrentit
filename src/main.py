#!/usr/local/bin/python3

from telethon import TelegramClient, events, Button
from telethon.tl.types import InputPeerChat, MessageMediaWebPage, PeerUser
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename
import traceback
import typing

import libtorrent as lt
import asyncio
import re
from urlextract import URLExtract
from io import BytesIO
import aiohttp

import time
import os
import logger as _log
import fex
import torrent_content as tc
import const

bot_token = os.getenv('BOT_TOKEN')
api_id = int(os.getenv('API_ID'))
api_hash = os.getenv('API_HASH')

client = TelegramClient('toby', api_id, api_hash)
bot = TelegramClient('bot', api_id, api_hash).start(bot_token=bot_token)
session = None

in_progress_users = set()
tasks = dict()
pending_torrents = dict()
l = _log.new_logger()


class File():
    def __init__(self, file_info):
        self.fullpath = file_info[0]
        self.num_pieces = file_info[1][0]
        self.size = file_info[1][1]

    @property
    def name(self):
        return os.path.basename(self.fullpath)


def files_size(files):
    files_size_sum = 0
    for f in files:
        files_size_sum += f.size

    return files_size_sum


class TorrentFileList():
    def __init__(self, torrent_name, files):
        self.name = torrent_name
        self.files = files


class FileGenerator():
    def __init__(self, torrent_handle, files, callback):
        self.files = iter(files)
        self.callback = callback
        self.torrent_handle = torrent_handle

    def next_file(self):
        file_info = next(self.files)
        if file_info.size > const.TG_MAX_FILE_SIZE:
            files = [{}]
            # return ZipTorrentContentFile(files)
        else:
            file = tc.TorrentContentFile(self.torrent_handle, file_info.num_pieces, file_info.size, self.callback)


@bot.on(events.NewMessage(pattern='/start'))
async def send_welcome(event):
    await bot.send_message(event.sender_id, 'Send me a torrent and i\'ll try download it for u')
    await asyncio.sleep(3)
    await bot.send_message(event.sender_id, '''**How i can retrieve torrents ?**
You can download your torrents content from Telegram ('`over Telegram`' button)
or in case of '`over Web`' button pressed from https://fex.net site.
But i recommend using '`over Web`' button because it's will be much faster.
In case of downloading over Telegram torrents will be zipped by default.
If u selected telegram and torrent content size is bigger then 1 GB
than zip file will be split into multipart archive(read https://telegra.ph/How-to-open-multipart-zip-archive-01-07)

**Can i send u any torrents ?**
Any normall torrents except NSFW, u will be banned if torrent content contain it.

**What is torrent content max size ?**
Max size is 20 GB for web upload and 5 GB for telegram upload.

**What speed for uploading to Telegram ?**
About 0.5 MB/s. So i recommend u pressing '`over Web`' button.
    ''', parse_mode='md')


url_re = re.compile('magnet:\?xt=urn:btih:[^ ]+')
extractor = URLExtract()


def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


# async def progress_callback(cur_piece, cur_file_num_pieces, downloaded_pieces_count, num_pieces, status_msg, status_msg_text):
#     piece = downloaded_pieces_count + cur_piece
#     percent = round(piece*100/num_pieces)
#     if cur_file_num_pieces == 0:
#         cur_file_percent = 100
#     else:
#         cur_file_percent = round(cur_piece*100/cur_file_num_pieces)
#
#     if cur_piece != 0:
#         prev_percent = round((piece-1)*100/num_pieces)
#         if cur_file_num_pieces == 0:
#             prev_cur_file_percent = 0
#         else:
#             prev_cur_file_percent = round(cur_piece-1*100/cur_file_num_pieces)
#         if prev_percent < percent or prev_cur_file_percent < cur_file_percent:
#             msg_text = status_msg_text.format(progress=percent, cur_file_progress=100 if cur_file_percent > 100 else cur_file_percent)
#             if percent == 100:
#                 msg_text += '\nComplete!'
#
#                 await status_msg.edit(msg_text, buttons=None)
#             else:
#                 await status_msg.edit(msg_text)
#     else:
#         msg_text = status_msg_text.format(progress=percent, cur_file_progress=100 if cur_file_percent > 100 else cur_file_percent)
#         if percent == 100:
#             msg_text += '\nComplete!'
#
#             await status_msg.edit(msg_text, buttons=None)
#         else:
#             await status_msg.edit(msg_text)
#
# async def progress_callback2(cur_piece, num_pieces, status_msg, status_msg_text):
#     piece = cur_piece
#     percent = round(piece*100/num_pieces)
#
#     if cur_piece != 0:
#         prev_percent = round((piece-1)*100/num_pieces)
#         if prev_percent < percent:
#             await status_msg.edit(status_msg_text.format(progress=percent))
#     else:
#         await status_msg.edit(status_msg_text.format(progress=percent))


@bot.on(events.CallbackQuery(pattern='^-?[0-9]+$'))
async def on_cancel_button(event):
    try:
        if event.sender_id in tasks:
            if not tasks[event.sender_id].cancelled():
                tasks[event.sender_id].cancel()
            del tasks[event.sender_id]
        msg = await event.get_message()
        await event.edit(msg.text + '\nCanceled!')
    except Exception as e:
        l.exception(e)


@bot.on(events.CallbackQuery(pattern='^-?[0-9]+:-?[0-9]+$'))
async def on_button(event):
    global session
    try:
        l.debug(event)
        button_id = None
        zfile = None
        idlog = _log.new_logger(user_id=event.sender_id)
        #user = await client.get_entity(event.sender_id)

        if event.sender_id in in_progress_users:
            #idlog.info(user.username + ' ' + 'Button pressed while' +
            #           ' ' + ('being in progress' if event.sender_id in in_progress_users else 'in pending state'))
            idlog.debug('in_progress_users ' + str(in_progress_users))
            idlog.debug('pending_torrents ' + str(pending_torrents))
            await event.reply('Wait until current torrent will be downloaded')
            return

        in_progress_users.add(event.sender_id)
        nidlog = None

        if event.sender_id not in pending_torrents or int(event.data.split(b':')[1]) != pending_torrents[event.sender_id][0][
            1]:
            # handle torrent request from old message
            idlog.info('request torrent handle from old message')
            event.message = await (await event.get_message()).get_reply_message()
            idlog.debug('reply message ' + str(event.message))
            magnet_link_or_bytes = await get_torrent_from_event(idlog, event)
            if magnet_link_or_bytes is None:
                idlog.warning('Not magnet or torrent in old message')
                try:
                    in_progress_users.remove(event.sender_id)
                    await event.reply('Failed resolve torrent')
                except:
                    pass
                return

            th = None
            try:
                th = await get_torrent_handle(idlog, magnet_link_or_bytes)
            except NoMetadataError:
                await event.edit('Couldn\'t resolve magnet link for metadata, torrent was removed from downloads. Sorry')
                del pending_torrents[event.sender_id]
                in_progress_users.remove(event.sender_id)
                return
            except Exception as e:
                idlog.exception(e)
                await event.edit('Error occured')
                del pending_torrents[event.sender_id]
                in_progress_users.remove(event.sender_id)
                return
            th.calc_prioritized_piece_count()
            th.prepare_pieces_priority()

            nidlog = _log.new_logger(torrent_name=th.name(), user_id=event.sender_id)

            s = th.status()
            files_info = th.files()
            files = [File(f) for f in files_info]
            nidlog.debug(files_info)

            #status_msg_text, status_msg_current_file_text, zips = prepare_status_message(files, s.name, th.total_size())
#            zfile = prepare_zip_file(th, s.name, files, event, status_msg_text, status_msg_current_file_text, event, nidlog)
            zfile = prepare_zip_file(th, s.name, files, event, nidlog)
            #zfile.progress_text = zips
            button_id, _ = event.data.split(b':')

        elif event.sender_id in pending_torrents:
            zfile, origin_msg_id = pending_torrents[event.sender_id][0]
            nidlog = _log.new_logger(torrent_name=zfile.torrent_handler.name(), user_id=event.sender_id)
            del pending_torrents[event.sender_id]

            button_id, msg_id = event.data.split(b':')
            #if int(msg_id) != origin_msg_id:
                # button was pressed on old torrent when new torrent was added recently
            #    nidlog.info('button was pressed on old torrent when new torrent was added recently')
            #    session.remove_torrent(zfile.torrent_handler)

        if button_id == b'1':
            # via Telegram
            nidlog.info('via Telegram')
            try:
                # await event.edit((await event.get_message()).message, buttons=[Button.inline('Cancel', str(event.sender_id))])
                await zfile.progress_callback(0)
                # await event.edit((await event.get_message()).message)
                zfile.event = event
                upload_task = client.loop.create_task(upload_all_torrent_content(zfile, event, nidlog))
                tasks[event.sender_id] = upload_task
                try:
                    await upload_task
                except asyncio.CancelledError as e:
                    nidlog.info('Canceled')


                if not upload_task.cancelled():
                    # TODO receive progress_text somehow another way
                    nidlog.info('Completed!')
                    _files = [File(f) for f in zfile.torrent_handler.files()]
                    progress_text = prepare_status_message(_files, zfile.torrent_handler.name(), zfile.torrent_handler.total_size())
                    await event.edit(progress_text.format(100))
                # size = zfile.size
                # f = open('test.zip', 'wb')
                # d = await zfile.read(500*1024)
                # while len(d) > 0:
                #    f.write(d)
                #    await asyncio.sleep(0.1)
                #    d = await zfile.read(500*1024)
                # f.close()

            except tc.NoActivityTimeoutError:
                await event.edit('Couldn\'t find any peers, torrent was removed from downloads. Sorry')
            except Exception as e:
                nidlog.exception(e)
            finally:
                zfile.close()
                session.remove_torrent(zfile.torrent_handler)

        elif button_id == b'2':
            # via Web
            nidlog.info('via Web')
            try:
                # url = 'https://api.anonfiles.com/upload'
                # url = 'http://127.0.0.1'

                # r = await httpx.post(url=url, files={'file': SyncZipTorrentContentFile(zfile)})
                # print(r.text)
                # curl = await asyncio.create_subprocess_exec('/usr/bin/curl',
                #                            '-T',
                #                            '-',
                #                            '-X',
                #                            'POST',
                #                            'https://api.anonfiles.com/upload',
                #                            stdout=asyncio.subprocess.PIPE,
                #                           stdin=asyncio.subprocess.PIPE, loop=client.loop)
                # await asyncio.sleep(5)
                # d = await zfile.read()
                # while d:
                #    print('curl write ', len(d))
                #    curl.stdin.write(d)
                #    await curl.stdin.drain()
                #    d = await zfile.read()
                # print('curl close stdin')
                # curl.stdin.close()
                # ret = await curl.stdout.read()
                # print(ret)
                # curl.kill()

                #                headers={
                #    'Content-Length': str(zfile.size + 195 + 2*len(zfile.name.encode('utf')))
                # }
                #                data = aiohttp.FormData(quote_fields=False)
                #                data.add_field('file', zfile, filename=zfile.name)

                #                async with aiohttp.ClientSession() as ses:
                #                    print('begin file uploading')
                #                    async with ses.post(url, data=data, headers=headers)  as resp:
                #                        print(await resp.text())
                await event.edit((await event.get_message()).message,
                                 buttons=[Button.inline('Cancel', str(event.sender_id))])
                zfile.set_should_split(False)
                fu = await fex.FexUploader.new()
                upload_task = client.loop.create_task(fu.add_file(zfile.name, zfile.size, zfile))
                tasks[event.sender_id] = upload_task
                try:
                    await upload_task
                except asyncio.CancelledError as e:
                    nidlog.info('Canceled')
                # await fu.add_file(zfile.name, zfile.size, zfile)

                if not upload_task.cancelled():
                    nidlog.info('Download url ' + fu.download_link)

                    # TODO receive progress_text somehow another way
                    _files = [File(f) for f in zfile.torrent_handler.files()]
                    progress_text = prepare_status_message(_files, zfile.torrent_handler.name(), zfile.torrent_handler.total_size())
                    await event.edit(progress_text.format(100),
                                     buttons=[Button.url('Download content', fu.download_link)])

                # await upload_to_ipfs(zfile)
            except tc.NoActivityTimeoutError:
                await event.edit('Couldn\'t find any peers, torrent was removed from downloads. Sorry')
            except Exception as e:
                nidlog.exception(e)
            finally:
                await fu.delete()
                zfile.close()
                session.remove_torrent(zfile.torrent_handler)
        # await event.edit((await event.get_message()).message,buttons=[Button.url('Download content', 'http://206.189.63.205:8080/'+str(file_key))])
        elif button_id == b'3':
            # upload via Web raw
            nidlog.info('via Web raw')
            try:
                fu = await fex.FexUploader.new(nidlog)
                await event.edit((await event.get_message()).message,
                                 buttons=[Button.inline('Cancel', str(event.sender_id))])

                files = []
                relative_size = 0
                _files = [File(f) for f in zfile.torrent_handler.files()]
                progress_text = prepare_status_message(_files, zfile.torrent_handler.name(), zfile.torrent_handler.total_size())
                callback = lambda percent: \
                        event.edit(progress_text.format(percent), buttons=[Button.inline('Cancel', str(event.sender_id))])
                for f in zfile.files:
                    files.append(fex.FexFile(tc.AsyncTorrentContentFileWrapper(f,
                                                                               callback,
                                                                               relative_size,
                                                                               zfile.files_size_sum, nidlog),
                                             f.info.fullpath, f.info.size))
                    relative_size += f.info.size

                upload_task = client.loop.create_task(fu.upload_files(files))
                tasks[event.sender_id] = upload_task

                try:
                    await upload_task
                except asyncio.CancelledError as e:
                    nidlog.info('Canceled')

                if not upload_task.cancelled():
                    nidlog.info('Completed!')
                    nidlog.info('Download url ' + fu.download_link)
                    await event.edit(progress_text.format(100),
                                     buttons=[Button.url('Download content', fu.download_link)])

            except tc.NoActivityTimeoutError:
                await event.edit('Couldn\'t find any peers, torrent was removed from downloads. Sorry')
            except Exception as e:
                nidlog.exception(e)
            finally:
                await fu.delete()
                zfile.close()
                session.remove_torrent(zfile.torrent_handler)
        else:
            nidlog.error('Error: wrong button id ', button_id)

    except Exception as e:
        l.exception(e)
    finally:
        if event.sender_id in tasks:
            if not tasks[event.sender_id].cancelled():
                tasks[event.sender_id].cancel()
            del tasks[event.sender_id]
        if event.sender_id in in_progress_users:
            in_progress_users.remove(event.sender_id)
        if event.sender_id in pending_torrents:
            del pending_torrents[event.sender_id]


async def upload_files(log, event, uploader, files, progress_text, files_size_sum):
    uploaded_sum = 0
    for f in files:
        log.info('upload file ', f.info.fullpath)
        await uploader.add_file(f.info.fullpath, f.info.size,
                                tc.AsyncTorrentContentFileWrapper(f, event, progress_text, uploaded_sum,
                                                                  files_size_sum, log))
        uploaded_sum += f.info.size


async def share_content_with_user(event):
    try:
        if event.media is not None:
            l.debug('attrs ' + str(event.message.media.document.attributes))
            doc_name = event.message.media.document.attributes[0].file_name
            if 'zip' not in doc_name:
                event.message.media.document.attributes[0].file_name = doc_name[:len(doc_name) - 4] + '.zip' + doc_name[
                                                                                                               len(
                                                                                                                   doc_name) - 4:]
            to_id = int(event.message.message)
            await bot.send_file(to_id, event.message.file.id)
        else:
            l.error("no media from client")
    except Exception as e:
        l.exception(e)
    return


def prepare_status_message(files, torrent_name, torrent_total_size):
    status_msg_text = str()
    for i, fi in enumerate(files):
        if i < 5:
            status_msg_text += str(i + 1) + " " + fi.name[0:30] + ("..." if len(fi.name) > 30 else "") + " " + sizeof_fmt(fi.size) + "\n"

    if len(files) > 5:
        status_msg_text += "...\n"
        status_msg_text += "Files count: " + str(len(files)) + "\n"

    status_msg_text = torrent_name + "\nTotal size: " + sizeof_fmt(
        torrent_total_size) + "\n" + "Files:\n" + status_msg_text + "Overall progress: {}%"

    # status_msg_text += "{cur_file_progress}"
    # status_msg_current_file_text = "Current file progress: {{cur_file_progress}}%: {cur_file_name}\n"
    # status_msg_text += "Overall progress: {{progress}}%"

    # status_msg_text = torrent_name+"\nTotal size: "+sizeof_fmt(torrent_total_size)+"\n"+"Files:\n"+status_msg_text

    # return (status_msg_text, status_msg_current_file_text, zip_status_msg_text)
    return status_msg_text


# def prepare_zip_file(th, torrent_name, files, event, status_msg_text, status_msg_current_file_text, status_msg):

def prepare_zip_file(th, torrent_name, files, event, log):
    downloaded_pieces_count = 0
    torrent_files = []
    num_pieces = 0
    for fi in files:
        num_pieces += fi.num_pieces
        # for i, fi in enumerate(files):
        #     if i != 0:
        #         downloaded_pieces_count += files[i-1].num_pieces
        #     cur_status_msg_text = status_msg_text.format(cur_file_progress=status_msg_current_file_text.format(cur_file_name=fi.name[0:25] + ("..." if len(fi.name)>25 else "")))
        #     callback = lambda cur_piece, _num_pieces=fi.num_pieces, _downloaded_pieces_count=downloaded_pieces_count, __num_pieces=num_pieces, _status_msg=status_msg, _cur_status_msg_text=cur_status_msg_text: progress_callback(cur_piece, _num_pieces, _downloaded_pieces_count, __num_pieces, _status_msg, _cur_status_msg_text)

        torrent_files.append(tc.TorrentContentFile(th, fi, log))
    zip_progress_text = prepare_status_message(files, torrent_name, th.total_size())

    callback = lambda percent: \
        event.edit(zip_progress_text.format(percent), buttons=[Button.inline('Cancel', str(event.sender_id))])

    zfile = tc.ZipTorrentContentFile(th, torrent_files, torrent_name, callback, log, should_split=True)

    return zfile


async def upload_all_torrent_content(zfile, event, log):
    for i in range(0, zfile.zip_parts):
        await upload_torrent_content(zfile, event.sender_id, log)
        zfile.zip_num += 1


@bot.on(events.NewMessage)
async def on_message(event):
    if event.raw_text == '/start':
        return
    l.debug(event)
    # BOT_AGENT_CHAT_ID - client user id to bypass 50 MB limit
    if event.from_id == int(os.getenv('BOT_AGENT_CHAT_ID')):
        await share_content_with_user(event)
        return
    try:
        idlog = _log.new_logger(user_id=event.from_id)
        if event.from_id in in_progress_users or event.from_id in pending_torrents:
            #user = await client.get_entity(event.from_id)
            #idlog.info(user.username + ' ' + 'sent message while' +
            #           ' ' + ('being in progress' if event.from_id in in_progress_users else 'in pending state'))
            idlog.debug('in_progress_users ' + str(in_progress_users))
            idlog.debug('pending_torrents ' + str(pending_torrents))
            await event.reply('Wait until current torrent will be downloaded')
            return
        magnet_link_or_bytes = await get_torrent_from_event(idlog, event)
        if magnet_link_or_bytes is not None:
            # set dumb data until torrent will be resolved
            pending_torrents[event.from_id] = True
        else:
            return

        e = await event.reply('Resolving torrent, please wait...')
        th = None
        try:
            th = await get_torrent_handle(idlog, magnet_link_or_bytes)
        except NoMetadataError:
            await e.edit('Couldn\'t resolve magnet link for metadata, torrent was removed from downloads. Sorry')
            del pending_torrents[event.from_id]
            return
        except Exception as err:
            idlog.exception(err)
            await e.edit('Error occured')
            del pending_torrents[event.from_id]
            return
        nidlog = _log.new_logger(torrent_name=th.name(), user_id=event.from_id)
        nidlog.debug('Torrent handler created successfull')

        s = th.status()
        files_info = th.files()
        files = [File(f) for f in files_info]
        nidlog.debug(files_info)

        files_size_sum = files_size(files)
        nidlog.info('Content size: ' + sizeof_fmt(files_size_sum))

        if files_size_sum > 20 * 1024 * 1024 * 1024:
            await e.edit('Torrent is too big, max allowed size is 20 GB')
            del pending_torrents[event.from_id]
            session.remove_torrent(th)
            return

        buttons = []
        if files_size_sum <= 5*1024*1024*1024:
            buttons.append(Button.inline('over Telegram(slow)', '1:' + str(event.message.id)))
        # buttons.append(Button.inline('via Web(Zip)', '2:'+str(event.message.id)))
        # if len(files) <= MAX_LEN_FILES_FOR_RAW:
        buttons.append(Button.inline('over Web', '3:' + str(event.message.id)))

        # status_msg_text, status_msg_current_file_text, zip_progress_text = prepare_status_message(files, s.name, th.total_size())
        zip_progress_text = prepare_status_message(files, s.name, th.total_size())
        status_msg = await e.edit(zip_progress_text.format(0).format(progress=""), buttons=buttons)
        zfile = prepare_zip_file(th, s.name, files, status_msg, nidlog)
        #zfile.progress_text = zip_progress_text

        pending_torrents[event.from_id] = ((zfile, event.message.id), time.time())

        # if filze size < 2 GB allow upload via Telegram
        # otherwise only web allowed
        # if files_size_sum <= 2*1024*1024*1024 or len(files) <= MAX_LEN_FILES_FOR_RAW:
        #     nidlog.debug('zfile.size = ', zfile.size)
        #     pending_torrents[event.from_id] = ((zfile, event.message.id), time.time())
        #     return

        # Upload via Web without buttons
        # try:
        #     nidlog.info('Uploading via web')
        #     await status_msg.edit(status_msg.message, buttons=[Button.inline('Cancel', str(event.from_id))])
        #     in_progress_users.add(event.from_id)
        #     del pending_torrents[event.from_id]
        #     zfile.set_should_split(False)
        #     zfile.event = status_msg
        #     fu = await fex.FexUploader.new()
        #     upload_task = client.loop.create_task(fu.add_file(zfile.name, zfile.size, zfile))
        #     tasks[event.from_id] = upload_task
        #     try:
        #         await upload_task
        #     except asyncio.CancelledError as e:
        #         print(e)
        #     #await fu.add_file(zfile.name, zfile.size, zfile)
        #     await fu.delete()
        #     if not upload_task.cancelled():
        #         await status_msg.edit(zfile.progress_text.format(zfile.last_percent),buttons=[Button.url('Download content', fu.download_link)])
        #
        #     #await upload_to_ipfs(zfile)
        # except Exception as e:
        #     print(e)
        #     traceback.print_exc()
        # finally:
        #     if event.from_id in tasks:
        #         del tasks[event.from_id]
        #     zfile.close()
        #     session.remove_torrent(zfile.torrent_handler)
        #     in_progress_users.remove(event.from_id)

    except Exception as e:
        l.exception(e)


async def get_torrent_from_event(log, event):
    if not hasattr(event, 'message'):
        await event.reply('Not valid torrent.\nPlease send me torrent file, link or magnet link')
        log.info('Not valid torrent')
        return

    if hasattr(event.message, 'media') and event.message.media is not None and type(
            event.message.media) is not MessageMediaWebPage:
        if event.message.media.document.mime_type != 'application/x-bittorrent':
            log.info('Not a torrent file: ' + event.message.media.document.name)
            await event.reply('Not a torrent file.\nPlease send me a torrent file/link or magnet link')
            return
        else:
            log.info('Downloading torrent file: ' + event.message.media.document.attributes[0].file_name)
            if event.message.media.document.size > 5*1024*1024:
                log.info('Too big torrent file size = ' + sizeof_fmt(event.message.media.document.size))
                await event.reply('Too big torrent file, max size is 5 MB, try another')
                return

            torrent_file = BytesIO()
            await bot.download_media(event.message.media, torrent_file)
            torrent_file.seek(0)
            return torrent_file.read()

    if not hasattr(event.message, 'message') or event.message.message is None:
        log.info("Not a text message")
        return

    magnets = url_re.findall(event.message.message)
    if len(magnets) != 0:
        magnet = magnets[0]
        log.info('Downloading via magnet link: ' + magnet)
        return magnet

    urls = extractor.find_urls(event.message.message)

    if len(urls) == 0:
        log.info('Message without url: ' + event.message.message)
        await event.reply('Please send me torrent file, link or magnet link')
        return

    url = urls[0]
    log.info('Downloading url: ' + url)
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.headers['Content-Type'] != 'application/x-bittorrent':
                log.info('Not a torrent file link')
                await event.reply('Not a torrent file link.\nPlease send me torrent file, link or magnet link')
            else:
                return await resp.read()


class NoMetadataError(Exception):
    pass

async def get_torrent_handle(log, torrent):
    th = None
    wait_metatada_timeout = 60
    if isinstance(torrent, str):
        if torrent.startswith('magnet'):
            log.info('Downloading metadata...')
            th = lt.add_magnet_uri(session, torrent, {'save_path': './'})
            time_elasped = 0
            while (not th.has_metadata()):
                await asyncio.sleep(3)
                time_elasped += 3
                log.debug('Wait metadata, time elapsed = {}s'.format(str(time_elasped)))
                if time_elasped >= wait_metatada_timeout:
                    session.remove_torrent(th)
                    log.info('Magnet link resolve timeout')
                    raise NoMetadataError
            log.info('Got metadata, starting torrent download...')
        else:
            raise Exception('String not a magnet link')

    elif isinstance(torrent, bytes):
        bd = lt.bdecode(torrent)
        info = lt.torrent_info(bd)
        th = session.add_torrent({'ti': info, 'save_path': '.'})
    else:
        log.error('Torrent handler creating failed, not valid torrent: ', torrent)
        raise Exception("Not valid torrent")

    th.calc_prioritized_piece_count()
    th.prepare_pieces_priority()
    return th


BOT_ID = os.getenv('BOT_ID')

async def upload_torrent_content(file, userid, log):
    log.info("trying upload {} with size = {}".format(file.name, file.size))

    uploaded_file = await client.upload_file(file, file_size=file.size, file_name=file.name)
    # await client.send_file(BOT_ID, uploaded_file,
    #                       attributes=(DocumentAttributeFilename(file_name=file.name),), caption=str(userid))
    await client.send_file(BOT_ID, uploaded_file, caption=str(userid))


def setup_session(session):
    ses_settings = session.get_settings()
    ses_settings['cache_size'] = 1024
    ses_settings['active_downloads'] = 40

    # ses_settings['alert_mask'] = lt.alert.category_t.torrent_log_notification | lt.alert.category_t.peer_log_notification

    ses_settings['close_redundant_connections'] = False
    ses_settings['prioritize_partial_pieces'] = True
    ses_settings['support_share_mode'] = False
    session.apply_settings(ses_settings)

    session.add_dht_router("router.utorrent.com", 6881)
    session.add_dht_router("dht.transmissionbt.com", 6881)
    session.add_dht_router("router.bitcomet.com", 6881)
    session.add_dht_router("dht.aelitis.com", 6881)
    session.start_dht()

async def periodic_cleanup():
    period = 60
    while True:
        try:
            await asyncio.sleep(period)
            delete_keys = []
            for k, v in pending_torrents.items():
                if type(v) is tuple and (time.time() - v[1]) > period:
                    th = v[0][0].torrent_handler
                    l.info('Removing pending torrent {} after incativity timeout'.format(th.name()[:30]))
                    session.remove_torrent(th)
                    delete_keys.append(k)

            for k in delete_keys:
                del pending_torrents[k]
        except Exception as e:
            l.exception(e)


if __name__ == '__main__':
    try:
        session = lt.session({'listen_interfaces': '0.0.0.0:6881'})
        setup_session(session)

        client.start()
        client.loop.create_task(periodic_cleanup())
        client.run_until_disconnected()
    except Exception as e:
        l.exception(e)
