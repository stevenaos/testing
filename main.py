import discord
import sqlite3
import json
import os
import asyncio
from datetime import datetime, timezone, timedelta
from discord.ext import commands, tasks
from googleapiclient.discovery import build
from dateutil import parser
import pytz

# Load config
with open('config.json') as f:
    config = json.load(f)

# Setup YouTube API
youtube = build('youtube', 'v3', developerKey=config['YOUTUBE_API_KEY'])

# Setup Discord bot
intents = discord.Intents.default()
intents.messages = True
bot = commands.Bot(command_prefix='!', intents=intents)
webhook = discord.Webhook.partial(config['WEBHOOK_ID'], config['WEBHOOK_TOKEN'], adapter=discord.RequestsWebhookAdapter())

# Setup SQLite
db_path = os.path.join('data', 'db.sqlite')
if not os.path.exists('data'):
    os.makedirs('data')

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users
             (channelId TEXT, userName TEXT, chatCount INTEGER, points INTEGER, watchTime INTEGER, firstSeen TEXT, lastReported TEXT, youtubeAccount TEXT, discordId TEXT, PRIMARY KEY (channelId, userName))''')
conn.commit()

# Update user data
def update_user(channel_id, user_name):
    try:
        now = datetime.now(timezone.utc)
        c.execute('SELECT * FROM users WHERE channelId = ? AND userName = ?', (channel_id, user_name))
        user = c.fetchone()
        if user:
            chat_count = user[2] + 1
            points = user[3] + 1  # Menambah points setiap kali pengguna mengirim pesan (1 menit = 1 point)
            watch_time = points  # Setiap poin setara dengan 1 menit
            last_reported = parser.parse(user[6]) if user[6] else now
            c.execute('UPDATE users SET chatCount = ?, points = ?, watchTime = ?, lastReported = ? WHERE channelId = ? AND userName = ?', (chat_count, points, watch_time, now.isoformat(), channel_id, user_name))
            
            # Check if it's time to report points
            if (now - last_reported) >= timedelta(minutes=1):  # Setiap 1 menit
                webhook.send(f'{user_name} telah menonton selama {watch_time} menit (points).')
        else:
            c.execute('INSERT INTO users (channelId, userName, chatCount, points, watchTime, firstSeen, lastReported, youtubeAccount, discordId) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (channel_id, user_name, 1, 1, 1, now.isoformat(), now.isoformat(), None, None))
        conn.commit()
    except Exception as e:
        print(f'Error in update_user: {e}')

# Add YouTube account to user data
@bot.command()
async def add_yt(ctx, yt_account):
    try:
        user_name = str(ctx.author)
        discord_id = str(ctx.author.id)
        channel_id = config['CHANNEL_IDS'][0]  # Assuming a single channel ID for simplicity

        c.execute('SELECT * FROM users WHERE channelId = ? AND userName = ?', (channel_id, user_name))
        user = c.fetchone()
        if user:
            if user[7]:  # YouTube account already added
                await ctx.send(f'{user_name}, Anda sudah menambahkan akun YouTube Anda: {user[7]}')
            else:
                c.execute('UPDATE users SET youtubeAccount = ?, discordId = ? WHERE channelId = ? AND userName = ?', (yt_account, discord_id, channel_id, user_name))
                conn.commit()
                await ctx.send(f'{user_name}, akun YouTube Anda telah ditambahkan: {yt_account}')
        else:
            c.execute('INSERT INTO users (channelId, userName, chatCount, points, watchTime, firstSeen, lastReported, youtubeAccount, discordId) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (channel_id, user_name, 0, 0, 0, datetime.now(timezone.utc).isoformat(), None, yt_account, discord_id))
            conn.commit()
            await ctx.send(f'{user_name}, akun YouTube Anda telah ditambahkan: {yt_account}')
    except Exception as e:
        await ctx.send(f'Error in add_yt command: {e}')
        print(f'Error in add_yt command: {e}')

# Check user points
@bot.command()
async def points(ctx, member: discord.Member = None):
    try:
        if member is None:
            member = ctx.author

        user_name = str(member)
        discord_id = str(member.id)
        channel_id = config['CHANNEL_IDS'][0]  # Assuming a single channel ID for simplicity

        c.execute('SELECT * FROM users WHERE channelId = ? AND (userName = ? OR discordId = ?)', (channel_id, user_name, discord_id))
        user = c.fetchone()
        if user:
            await ctx.send(f'{member.mention}, Anda memiliki {user[3]} points.')
        else:
            await ctx.send(f'{member.mention}, Anda belum menambahkan akun YouTube Anda. Gunakan perintah !add_yt <url akun yt atau @username> untuk menambahkan.')
    except Exception as e:
        await ctx.send(f'Error in points command: {e}')
        print(f'Error in points command: {e}')

# Listen to live chat messages
async def listen_live_chat(channel_id):
    try:
        live_chat_id = await get_live_chat_id(channel_id)
        last_processed_time = None

        while True:
            try:
                request = youtube.liveChatMessages().list(
                    liveChatId=live_chat_id,
                    part='snippet,authorDetails',
                    maxResults=200
                )
                response = request.execute()
                messages = response.get('items', [])
                
                for message in messages:
                    published_time = parser.parse(message['snippet']['publishedAt'])
                    if last_processed_time is None or published_time > last_processed_time:
                        user_name = message['authorDetails']['displayName']
                        if user_name.lower() != 'nightbot':  # Memeriksa apakah pengguna bukan "nightbot"
                            update_user(channel_id, user_name)
                            timestamp = int(published_time.timestamp())
                            message_time = f"<t:{timestamp}:R>"
                            print(f'{user_name}: {message["snippet"]["displayMessage"]}')
                            webhook.send(f"{message_time} | {user_name}: {message["snippet"]["displayMessage"]}")
                            last_processed_time = published_time
                            
                await asyncio.sleep(1)  # Fetch messages every 1 second (to avoid excessive API requests)
            except Exception as e:
                print(f'Error fetching live chat messages: {e}')
                await asyncio.sleep(10)  # Jika ada error, tunggu 10 detik sebelum mencoba lagi
    except Exception as e:
        print(f'Error in listen_live_chat: {e}')

async def get_live_chat_id(channel_id):
    try:
        request = youtube.search().list(
            part='snippet',
            channelId=channel_id,
            eventType='live',
            type='video'
        )
        response = request.execute()
        if response['items']:
            live_video_id = response['items'][0]['id']['videoId']
            video_request = youtube.videos().list(
                part='liveStreamingDetails,snippet',
                id=live_video_id
            )
            video_response = video_request.execute()
            return video_response['items'][0]['liveStreamingDetails']['activeLiveChatId']
        return None
    except Exception as e:
        print(f'Error fetching live chat ID: {e}')
        return None

async def get_live_streamer_name(channel_id):
    try:
        request = youtube.channels().list(
            part='snippet',
            id=channel_id
        )
        response = request.execute()
        return response['items'][0]['snippet']['title']
    except Exception as e:
        print(f'Error fetching live streamer name: {e}')
        return None

@bot.event
async def on_ready():
    print('Bot is ready')
    for channel_id in config['CHANNEL_IDS']:
        print(f'Listening to live chat for channel {channel_id}')
        bot.loop.create_task(listen_live_chat(channel_id))

# Command to send db.json file
@bot.command()
async def view_data(ctx):
    try:
        c.execute('SELECT * FROM users')
        users = c.fetchall()
        data = []
        for user in users:
            live_streamer_name = await get_live_streamer_name(user[0])
            first_seen = parser.parse(user[5]).astimezone(timezone(timedelta(hours=7)))  # Waktu Indonesia Barat (UTC+7)
            last_reported = parser.parse(user[6]).astimezone(timezone(timedelta(hours=7))) if user[6] else None
            user_data = {
                "liveStreamerName": live_streamer_name,
                "userName": user[1],
                "chatCount": user[2],
                "points": user[3],
                "watchTime": user[4],  # Watch time tetap di view data
                "firstSeen": first_seen.strftime("%H:%M %p | %d/%m/%Y"),
                "lastReported": last_reported.strftime("%H:%M %p | %d/%m/%Y") if last_reported else None,
                "youtubeAccount": user[7],
                "discordId": user[8]
            }
            data.append(user_data)

        file_path = os.path.join('data', 'db.json')
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=2)

        await ctx.send(file=discord.File(file_path))
    except Exception as e:
        await ctx.send(f'Error in view_data command: {e}')
        print(f'Error in view_data command: {e}')

# Discord bot login
bot.run(config['DISCORD_TOKEN'])
