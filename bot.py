import discord
from discord import app_commands
from flask import Flask, request, redirect, session, jsonify
import os
from os import environ
import asyncio
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Boolean, TIMESTAMP, text
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta
import threading
import logging
from dotenv import load_dotenv
import stripe
from contextlib import contextmanager

# Load environment variables from .env file
load_dotenv()

# Retrieve environment variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
SECRET_KEY = os.getenv('SECRET_KEY')
REDIRECT_URI = os.getenv('REDIRECT_URI')
DATABASE_URL = os.getenv('DATABASE_URL')

# Database setup
engine = create_engine(DATABASE_URL)
metadata = MetaData()
Session = sessionmaker(bind=engine)
db_session = Session()

# Initialize the Discord bot with intents
intents = discord.Intents.default()
intents.message_content = True

class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = MyBot()

# Flask app setup
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Stripe setup
stripe.api_key = STRIPE_SECRET_KEY

# Subscription tier requirements
tier_requirements = {
    "tier_A": 250,
    "tier_B": 500,
    "tier_C": 1000,
    "tier_D": 5000,
    "tier_E": float('inf')  # No upper limit
}

# Cooldown period (seconds)
COOLDOWN_PERIOD = 60  # 1 minute cooldown for demonstration purposes

def get_required_tier(member_count):
    if member_count <= tier_requirements["tier_A"]:
        return "tier_A"
    elif member_count <= tier_requirements["tier_B"]:
        return "tier_B"
    elif member_count <= tier_requirements["tier_C"]:
        return "tier_C"
    elif member_count <= tier_requirements["tier_D"]:
        return "tier_D"
    else:
        return "tier_E"

# Define tables using SQLAlchemy
users = Table(
    'users', metadata,
    Column('id', Integer, primary_key=True),
    Column('discord_id', String(30), nullable=False),
    Column('username', String(100)),
    Column('verification_status', Boolean, default=False),
    Column('last_verification_attempt', TIMESTAMP)
)

servers = Table(
    'servers', metadata,
    Column('id', Integer, primary_key=True),
    Column('server_id', String(30), nullable=False, unique=True),
    Column('owner_id', String(30), nullable=False),
    Column('role_id', String(30), nullable=False),
    Column('tier', String(50), server_default=text("'tier_A'"), nullable=False),
    Column('subscription_status', Boolean, default=False)
)

command_usage = Table(
    'command_usage', metadata,
    Column('id', Integer, primary_key=True),
    Column('server_id', String(30), nullable=False),
    Column('user_id', String(30), nullable=False),
    Column('command', String(50), nullable=False),
    Column('timestamp', TIMESTAMP, nullable=False)
)

# Create tables in the database
metadata.create_all(engine)

# Fetch server configuration from the database
def get_server_config(guild_id):
    server_config = db_session.query(servers).filter_by(server_id=str(guild_id)).first()
    return server_config

# Fetch user verification status from the database
def get_user_verification_status(discord_id):
    user = db_session.query(users).filter_by(discord_id=str(discord_id)).first()
    return user

# Update user verification status in the database
def update_user_verification_status(discord_id, status):
    user = db_session.query(users).filter_by(discord_id=str(discord_id)).first()
    if user:
        user.verification_status = status
        db_session.commit()

@contextmanager
def session_scope():
    session = Session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()

# Track user verification attempts in the database
def track_verification_attempt(discord_id):
    with session_scope() as session:
        user = session.query(users).filter_by(discord_id=str(discord_id)).first()
        if user:
            session.execute(
                users.update().
                where(users.c.discord_id == str(discord_id)).
                values(last_verification_attempt=datetime.utcnow())
            )
        else:
            insert_stmt = users.insert().values(
                discord_id=str(discord_id),
                verification_status=False,
                last_verification_attempt=datetime.utcnow()
            )
            session.execute(insert_stmt)

# Track command usage in the database for analytics
def track_command_usage(server_id, user_id, command):
    insert_stmt = command_usage.insert().values(
        server_id=str(server_id),
        user_id=str(user_id),
        command=command,
        timestamp=datetime.utcnow()
    )
    db_session.execute(insert_stmt)
    db_session.commit()

# Check if user is within the cooldown period
def is_user_in_cooldown(discord_id):
    user = db_session.query(users).filter_by(discord_id=str(discord_id)).first()
    if user and user.last_verification_attempt:
        cooldown_end = user.last_verification_attempt + timedelta(seconds=COOLDOWN_PERIOD)
        if datetime.utcnow() < cooldown_end:
            return True
    return False

# Check if server meets the tier requirement
def check_tier_requirements(guild):
    server_config = get_server_config(guild.id)
    if server_config:
        member_count = guild.member_count
        tier_limit = tier_requirements[server_config.tier]
        if member_count > tier_limit:
            return False, tier_limit
    return True, None

async def assign_role(guild_id, user_id, role_id):
    guild = bot.get_guild(int(guild_id))
    member = guild.get_member(int(user_id))
    role = guild.get_role(int(role_id))
    if member and role:
        await member.add_roles(role)

# Configure logging
logging.basicConfig(level=logging.INFO)

async def generate_stripe_verification_url(guild_id, user_id, role_id, channel_id):
    try:
        verification_session = stripe.identity.VerificationSession.create(
            type='document',
            metadata={
                'guild_id': guild_id,
                'user_id': user_id,
                'role_id': role_id,
                'channel_id': channel_id
            }
        )
        return verification_session.url
    except stripe.error.StripeError as e:
        logging.error(f"Stripe API error: {str(e)}")
        return None

@bot.tree.command(name="verify", description="Start the verification process")
async def verify(interaction: discord.Interaction):
    logging.info(f"Received /verify command from user {interaction.user} in guild {interaction.guild}")
    guild_id = str(interaction.guild_id)
    member_count = interaction.guild.member_count

    required_tier = get_required_tier(member_count)
    server_config = get_server_config(guild_id)

    if not server_config:
        await interaction.response.send_message("No configuration found for this server. Please ask an admin to set up the server using `/set_role`.", ephemeral=True)
        return

    if not server_config.subscription_status:
        logging.warning(f"No active subscription for guild {guild_id}")
        await interaction.response.send_message("This server does not have an active subscription.", ephemeral=True)
        return

    subscribed_tier = server_config.tier

    if subscribed_tier not in tier_requirements:
        logging.warning(f"Invalid subscription tier {subscribed_tier} for guild {guild_id}")
        await interaction.response.send_message("Invalid subscription tier configured for this server. Please ask an admin to correctly subscribe to the appropriate tier.", ephemeral=True)
        return

    if tier_requirements[subscribed_tier] < tier_requirements[required_tier]:
        logging.warning(f"Subscription tier {subscribed_tier} does not cover {member_count} members")
        await interaction.response.send_message(f"This server's subscription ({subscribed_tier}) does not cover {member_count} members. Please upgrade to {required_tier}.", ephemeral=True)
        return

    if is_user_in_cooldown(interaction.user.id):
        logging.info(f"User {interaction.user.id} is in cooldown period")
        await interaction.response.send_message(f"You are currently in a cooldown period. Please wait before attempting to verify again.", ephemeral=True)
        return

    user = get_user_verification_status(interaction.user.id)
    if user and user.verification_status:
        logging.info(f"User {interaction.user.id} is already verified")
        await assign_role(guild_id, interaction.user.id, server_config.role_id)
        await interaction.response.send_message("You are already verified. Role has been assigned.", ephemeral=True)
        return

    verification_url = await generate_stripe_verification_url(
        guild_id,
        interaction.user.id,
        server_config.role_id,
        str(interaction.channel_id))
    
    if not verification_url:
        await interaction.response.send_message("Failed to initiate verification process. Please try again later or contact support.", ephemeral=True)
        return

    track_verification_attempt(interaction.user.id)
    track_command_usage(guild_id, interaction.user.id, "verify")
    logging.info(f"Generated verification URL for user {interaction.user.id}: {verification_url}")
    await interaction.response.send_message(f"Click the link below to verify your age: {verification_url}", ephemeral=True)

@bot.tree.command(name="reverify", description="Start the reverification process")
async def reverify(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    member_count = interaction.guild.member_count

    required_tier = get_required_tier(member_count)
    server_config = get_server_config(guild_id)

    if not server_config:
        await interaction.response.send_message("No configuration found for this server. Please ask an admin to set up the server using `/set_role`.", ephemeral=True)
        return

    if not server_config.subscription_status:
        await interaction.response.send_message("This server does not have an active subscription.", ephemeral=True)
        return

    subscribed_tier = server_config.tier

    if subscribed_tier not in tier_requirements:
        await interaction.response.send_message("Invalid subscription tier configured for this server. Please ask an admin to correctly subscribe to the appropriate tier.", ephemeral=True)
        return

    if tier_requirements[subscribed_tier] < tier_requirements[required_tier]:
        await interaction.response.send_message(f"This server's subscription ({subscribed_tier}) does not cover {member_count} members. Please upgrade to {required_tier}.", ephemeral=True)
        return

    if is_user_in_cooldown(interaction.user.id):
        await interaction.response.send_message(f"You are currently in a cooldown period. Please wait before attempting to verify again.", ephemeral=True)
        return

    verification_url = await generate_stripe_verification_url(
        guild_id,
        interaction.user.id,
        server_config.role_id,
        str(interaction.channel_id))

    if not verification_url:
        await interaction.response.send_message("Failed to initiate verification process. Please try again later or contact support.", ephemeral=True)
        return

    track_verification_attempt(interaction.user.id)
    track_command_usage(guild_id, interaction.user.id, "reverify")
    await interaction.response.send_message(f"Click the link below to verify your age: {verification_url}", ephemeral=True)

@bot.tree.command(name="set_role", description="Set the role for verified users")
@app_commands.describe(role="The role to assign to verified users")
async def set_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    server_config = get_server_config(guild_id)

    if not server_config:
        db_session.execute(servers.insert().values(
            server_id=guild_id,
            owner_id=str(interaction.guild.owner_id),
            role_id=str(role.id),
            subscription_status=True  # Assuming subscription is active for testing
        ))
        db_session.commit()
    else:
        server_config.role_id = str(role.id)
        db_session.commit()

    await interaction.response.send_message(f"Role for verification set to: {role.name}", ephemeral=True)

@bot.tree.command(name="set_subscription", description="Set the subscription tier for the server")
@app_commands.describe(tier="The subscription tier to set")
async def set_subscription(interaction: discord.Interaction, tier: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    server_config = get_server_config(guild_id)

    if not server_config:
        await interaction.response.send_message("This server does not have an active subscription.", ephemeral=True)
        return

    if tier not in tier_requirements:
        await interaction.response.send_message(f"Invalid tier. Available tiers: {', '.join(tier_requirements.keys())}", ephemeral=True)
        return

    server_config.tier = tier
    db_session.commit()

    await interaction.response.send_message(f"Subscription tier set to: {tier}", ephemeral=True)

@bot.tree.command(name="ping", description="Check if the bot is responsive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

@bot.event
async def on_ready():
    logging.info(f'Bot is ready. Logged in as {bot.user}')
    try:
        synced = await bot.tree.sync()
        logging.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}")

@app.route('/stripe_webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    if event['type'] == 'identity.verification_session.verified':
        session = event['data']['object']
        guild_id = session['metadata']['guild_id']
        user_id = session['metadata']['user_id']
        role_id = session['metadata']['role_id']
        
        # Assign role and update verification status
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(assign_role(guild_id, user_id, role_id))
        update_user_verification_status(user_id, True)
        
        return 'Verification successful, role assigned!', 200
    elif event['type'] == 'identity.verification_session.canceled':
        session = event['data']['object']
        guild_id = session['metadata']['guild_id']
        user_id = session['metadata']['user_id']
        channel_id = session['metadata']['channel_id']
        
        # Get the Discord user
        guild = bot.get_guild(int(guild_id))
        member = guild.get_member(int(user_id)) if guild else None
        user_mention = member.mention if member else f"User (ID: {user_id})"
        
        # Send cancellation message
        message = f"Verification canceled by {user_mention}"
        run_coroutine_in_new_loop(send_discord_message(channel_id, message))
        
        return 'Verification canceled, message sent', 200

    return '', 200

async def send_discord_message(channel_id, message):
    channel = bot.get_channel(int(channel_id))
    if channel:
        await channel.send(message)

def run_coroutine_in_new_loop(coroutine):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coroutine)

@app.route('/analytics')
def analytics():
    result = db_session.query(command_usage).all()
    analytics_data = [{"server_id": row.server_id, "user_id": row.user_id, "command": row.command, "timestamp": row.timestamp} for row in result]
    return jsonify(analytics_data)

def run_flask():
    app.run(port=5000)

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    bot.run(DISCORD_BOT_TOKEN)