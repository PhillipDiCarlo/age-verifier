import discord
from discord.ext import commands
from discord.ext.commands import BucketType
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
import onfido
import urllib3

# print(onfido.__version__)

# Load environment variables from .env file
load_dotenv()

# Retrieve environment variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
ONFIDO_API_TOKEN = os.getenv('ONFIDO_API_TOKEN')
ONFIDO_WORKFLOW_ID = os.getenv('ONFIDO_WORKFLOW_ID')
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
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True  # Ensure message content intent is enabled
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Flask app setup
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Onfido client configuration
configuration = onfido.Configuration(
    api_token=environ['ONFIDO_API_TOKEN'],
    region=onfido.configuration.Region.US,
    timeout=urllib3.util.Timeout(connect=60.0, read=60.0)
)
# configuration.host = "https://api.us.onfido.com/v3.6"
onfido_api = onfido.DefaultApi(onfido.ApiClient(configuration))

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

SUPPORTED_COUNTRIES = {
    "ALB", "DZA", "AND", "ARG", "ARM", "AUS", "AUT", "AZE", "BHR", "BLR", "BEL", "BLZ", "BEN", "BMU", "BTN", "BIH",
    "BRA", "BRN", "BGR", "BFA", "BDI", "KHM", "CMR", "CAN", "CYM", "CAF", "CHL", "CHN", "COL", "COG", "CRI", "HRV",
    "CUB", "CYP", "CZE", "CIV", "DNK", "DJI", "DMA", "DOM", "ECU", "EGY", "SLV", "GNQ", "EST", "ETH", "FRO", "FJI",
    "FIN", "FRA", "GAB", "GEO", "DEU", "GIB", "GRC", "GRD", "GTM", "GGY", "GIN", "HND", "HKG", "HUN", "ISL", "IND",
    "IDN", "IRN", "IRL", "IMN", "ISR", "ITA", "JAM", "JPN", "JEY", "KAZ", "KEN", "KOR", "KWT", "KGZ", "LVA", "LBN",
    "LSO", "LIE", "LTU", "LUX", "MWI", "MYS", "MLI", "MLT", "MRT", "MEX", "MDA", "MCO", "MNE", "MSR", "MAR", "NAM",
    "NPL", "NLD", "NZL", "NIC", "NER", "NGA", "NFK", "NOR", "OMN", "PAK", "PSE", "PAN", "PRY", "PER", "PHL", "POL",
    "PRT", "PRI", "QAT", "RKS", "ROU", "RUS", "KNA", "LCA", "VCT", "WSM", "SAU", "SRB", "SGP", "SVK", "SVN", "SOM",
    "ZAF", "ESP", "LKA", "SDN", "SWE", "CHE", "SYR", "TWN", "TJK", "TZA", "THA", "TGO", "TTO", "TUN", "TUR", "TKM",
    "TCA", "TUV", "UGA", "UKR", "ARE", "GBR", "USA", "UZB", "VUT", "VEN", "VNM", "VGB", "ZMB", "ZWE", "ALA"
}

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

# Track user verification attempts in the database
def track_verification_attempt(discord_id):
    user = db_session.query(users).filter_by(discord_id=str(discord_id)).first()
    if user:
        user.last_verification_attempt = datetime.utcnow()
        db_session.commit()
    else:
        # If user does not exist, create one
        insert_stmt = users.insert().values(
            discord_id=str(discord_id),
            verification_status=False,
            last_verification_attempt=datetime.utcnow()
        )
        db_session.execute(insert_stmt)
        db_session.commit()

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


def locale_to_country_code(locale):
    locale_map = {
        'af': 'AFG', 'al': 'ALB', 'dz': 'DZA', 'as': 'ASM', 'ad': 'AND', 'ao': 'AGO', 'ai': 'AIA', 'aq': 'ATA',
        'ag': 'ATG', 'ar': 'ARG', 'am': 'ARM', 'aw': 'ABW', 'au': 'AUS', 'at': 'AUT', 'az': 'AZE', 'bs': 'BHS',
        'bh': 'BHR', 'bd': 'BGD', 'bb': 'BRB', 'by': 'BLR', 'be': 'BEL', 'bz': 'BLZ', 'bj': 'BEN', 'bm': 'BMU',
        'bt': 'BTN', 'bo': 'BOL', 'bq': 'BES', 'ba': 'BIH', 'bw': 'BWA', 'bv': 'BVT', 'br': 'BRA', 'io': 'IOT',
        'bn': 'BRN', 'bg': 'BGR', 'bf': 'BFA', 'bi': 'BDI', 'cv': 'CPV', 'kh': 'KHM', 'cm': 'CMR', 'ca': 'CAN',
        'ky': 'CYM', 'cf': 'CAF', 'td': 'TCD', 'cl': 'CHL', 'cn': 'CHN', 'cx': 'CXR', 'cc': 'CCK', 'co': 'COL',
        'km': 'COM', 'cg': 'COG', 'cd': 'COD', 'ck': 'COK', 'cr': 'CRI', 'hr': 'HRV', 'cu': 'CUB', 'cw': 'CUW',
        'cy': 'CYP', 'cz': 'CZE', 'ci': 'CIV', 'dk': 'DNK', 'dj': 'DJI', 'dm': 'DMA', 'do': 'DOM', 'ec': 'ECU',
        'eg': 'EGY', 'sv': 'SLV', 'gq': 'GNQ', 'er': 'ERI', 'ee': 'EST', 'et': 'ETH', 'fk': 'FLK', 'fo': 'FRO',
        'fj': 'FJI', 'fi': 'FIN', 'fr': 'FRA', 'gf': 'GUF', 'pf': 'PYF', 'tf': 'ATF', 'ga': 'GAB', 'gm': 'GMB',
        'ge': 'GEO', 'de': 'DEU', 'gh': 'GHA', 'gi': 'GIB', 'gr': 'GRC', 'gl': 'GRL', 'gd': 'GRD', 'gp': 'GLP',
        'gu': 'GUM', 'gt': 'GTM', 'gg': 'GGY', 'gn': 'GIN', 'gw': 'GNB', 'gy': 'GUY', 'ht': 'HTI', 'hm': 'HMD',
        'va': 'VAT', 'hn': 'HND', 'hk': 'HKG', 'hu': 'HUN', 'is': 'ISL', 'in': 'IND', 'id': 'IDN', 'ir': 'IRN',
        'iq': 'IRQ', 'ie': 'IRL', 'im': 'IMN', 'il': 'ISR', 'it': 'ITA', 'jm': 'JAM', 'jp': 'JPN', 'je': 'JEY',
        'jo': 'JOR', 'kz': 'KAZ', 'ke': 'KEN', 'ki': 'KIR', 'kp': 'PRK', 'kr': 'KOR', 'kw': 'KWT', 'kg': 'KGZ',
        'la': 'LAO', 'lv': 'LVA', 'lb': 'LBN', 'ls': 'LSO', 'lr': 'LBR', 'ly': 'LBY', 'li': 'LIE', 'lt': 'LTU',
        'lu': 'LUX', 'mo': 'MAC', 'mg': 'MDG', 'mw': 'MWI', 'my': 'MYS', 'mv': 'MDV', 'ml': 'MLI', 'mt': 'MLT',
        'mh': 'MHL', 'mq': 'MTQ', 'mr': 'MRT', 'mu': 'MUS', 'yt': 'MYT', 'mx': 'MEX', 'fm': 'FSM', 'md': 'MDA',
        'mc': 'MCO', 'mn': 'MNG', 'me': 'MNE', 'ms': 'MSR', 'ma': 'MAR', 'mz': 'MOZ', 'mm': 'MMR', 'na': 'NAM',
        'nr': 'NRU', 'np': 'NPL', 'nl': 'NLD', 'nc': 'NCL', 'nz': 'NZL', 'ni': 'NIC', 'ne': 'NER', 'ng': 'NGA',
        'nu': 'NIU', 'nf': 'NFK', 'mk': 'MKD', 'mp': 'MNP', 'no': 'NOR', 'om': 'OMN', 'pk': 'PAK', 'pw': 'PLW',
        'ps': 'PSE', 'pa': 'PAN', 'pg': 'PNG', 'py': 'PRY', 'pe': 'PER', 'ph': 'PHL', 'pn': 'PCN', 'pl': 'POL',
        'pt': 'PRT', 'pr': 'PRI', 'qa': 'QAT', 'xk': 'RKS', 'ro': 'ROU', 'ru': 'RUS', 'rw': 'RWA', 're': 'REU',
        'bl': 'BLM', 'sh': 'SHN', 'kn': 'KNA', 'lc': 'LCA', 'mf': 'MAF', 'pm': 'SPM', 'vc': 'VCT', 'ws': 'WSM',
        'sm': 'SMR', 'st': 'STP', 'sa': 'SAU', 'sn': 'SEN', 'rs': 'SRB', 'sc': 'SYC', 'sl': 'SLE', 'sg': 'SGP',
        'sx': 'SXM', 'sk': 'SVK', 'si': 'SVN', 'sb': 'SLB', 'so': 'SOM', 'za': 'ZAF', 'gs': 'SGS', 'ss': 'SSD',
        'es': 'ESP', 'lk': 'LKA', 'sd': 'SDN', 'sr': 'SUR', 'sj': 'SJM', 'se': 'SWE', 'ch': 'CHE', 'sy': 'SYR',
        'tw': 'TWN', 'tj': 'TJK', 'tz': 'TZA', 'th': 'THA', 'tl': 'TLS', 'tg': 'TGO', 'tk': 'TKL', 'to': 'TON',
        'tt': 'TTO', 'tn': 'TUN', 'tr': 'TUR', 'tm': 'TKM', 'tc': 'TCA', 'tv': 'TUV', 'ug': 'UGA', 'ua': 'UKR',
        'ae': 'ARE', 'gb': 'GBR', 'um': 'UMI', 'us': 'USA', 'uy': 'URY', 'uz': 'UZB', 'vu': 'VUT', 've': 'VEN',
        'vn': 'VNM', 'vg': 'VGB', 'vi': 'VIR', 'wf': 'WLF', 'eh': 'ESH', 'ye': 'YEM', 'zm': 'ZMB', 'zw': 'ZWE',
        'sz': 'SWZ', 'ax': 'ALA'
    }
    return locale_map.get(locale.lower().split('-')[0], 'USA')  # Default to USA if not found

async def assign_role(guild_id, user_id, role_id):
    guild = bot.get_guild(int(guild_id))
    member = guild.get_member(int(user_id))
    role = guild.get_role(int(role_id))
    if member and role:
        await member.add_roles(role)

# Configure logging
logging.basicConfig(level=logging.INFO)

# Add logging inside your commands
@bot.command()
@commands.cooldown(1, COOLDOWN_PERIOD, BucketType.user)
async def verify(ctx):
    logging.info(f"Received !verify command from user {ctx.author} in guild {ctx.guild}")
    guild_id = str(ctx.guild.id)
    member_count = ctx.guild.member_count

    required_tier = get_required_tier(member_count)
    server_config = get_server_config(guild_id)

    if not server_config:
        await ctx.send("No configuration found for this server. Please ask an admin to set up the server using `!set_role @Role` and `!set_subscription [tier]`.")
        return

    if not server_config.subscription_status:
        logging.warning(f"No active subscription for guild {guild_id}")
        await ctx.send("This server does not have an active subscription.")
        return

    subscribed_tier = server_config.tier

    if subscribed_tier not in tier_requirements:
        logging.warning(f"Invalid subscription tier {subscribed_tier} for guild {guild_id}")
        await ctx.send("Invalid subscription tier configured for this server. Please ask an admin to correct it using `!set_subscription [tier]`.")
        return

    if tier_requirements[subscribed_tier] < tier_requirements[required_tier]:
        logging.warning(f"Subscription tier {subscribed_tier} does not cover {member_count} members")
        await ctx.send(f"This server's subscription ({subscribed_tier}) does not cover {member_count} members. Please upgrade to {required_tier}.")
        return

    if is_user_in_cooldown(ctx.author.id):
        logging.info(f"User {ctx.author.id} is in cooldown period")
        await ctx.send(f"You are currently in a cooldown period. Please wait before attempting to verify again.")
        return

    user = get_user_verification_status(ctx.author.id)
    if user and user.verification_status:
        logging.info(f"User {ctx.author.id} is already verified")
        await assign_role(guild_id, ctx.author.id, server_config.role_id)
        await ctx.send("You are already verified. Role has been assigned.")
        return

    # Get the user's locale
    # user_locale = str(ctx.author.locale) if ctx.author.locale else 'en-US'
    user_locale = 'en-US' # TODO: Temp Solution
    country_code = locale_to_country_code(user_locale)

    if country_code not in SUPPORTED_COUNTRIES:
        await ctx.send(f"Sorry, the verification bot is not available in your country ({country_code}). Verification cannot proceed.")
        return

    verification_url = await generate_onfido_verification_url(guild_id, ctx.author.id, server_config.role_id, user_locale)
    
    if not verification_url:
        await ctx.send("Failed to initiate verification process. Please try again later or contact support.")
        return

    track_verification_attempt(ctx.author.id)
    track_command_usage(guild_id, ctx.author.id, "verify")
    logging.info(f"Generated verification URL for user {ctx.author.id}: {verification_url}")
    await ctx.send(f"This server has {member_count} members. Click the link below to verify your age: {verification_url}")

# Update the generate_onfido_verification_url function to use the country code:
async def generate_onfido_verification_url(guild_id, user_id, role_id, user_locale):
    try:
        # Convert Discord locale to country code
        country_code = locale_to_country_code(user_locale)

        # Create an applicant
        applicant = onfido_api.create_applicant(
            onfido.ApplicantBuilder(
                first_name="Discord",
                last_name="User",
                external_id=f"{guild_id}-{user_id}-{role_id}",
                location=onfido.LocationBuilder(
                    country_of_residence=country_code
                ),
                consents=onfido.ConsentsBuilder(
                    privacy_notices_read=True
                )
            )
        )
        
        logging.info(f"Onfido API response (applicants): {applicant}")

        # Create a check
        check = onfido_api.create_check(
            onfido.CheckBuilder(
                applicant_id=applicant.id,
                report_names=["identity_enhanced"],
                consider=None,
                # workflow_id=ONFIDO_WORKFLOW_ID,
                async_=True
            )
        )
        
        logging.info(f"Onfido API response (checks): {check}")

        # Generate SDK token
        sdk_token = onfido_api.generate_sdk_token(
            onfido.SdkTokenBuilder(
                applicant_id=applicant.id,
                referrer="*://*/*",
                # workflow_id=ONFIDO_WORKFLOW_ID
            )
        )

        # Use the SDK token to create the verification URL
        verification_url = f"https://id.onfido.com/start_iframe?sdk_token={sdk_token.token}"

        return verification_url

    except onfido.ApiException as e:
        logging.error(f"Failed to create Onfido applicant or check: {e}")
        logging.error(f"Response body: {e.body}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error in generate_onfido_verification_url: {e}")
        return None


# Update the reverify command similarly:
@bot.command()
@commands.cooldown(1, COOLDOWN_PERIOD, BucketType.user)
async def reverify(ctx):
    guild_id = str(ctx.guild.id)
    member_count = ctx.guild.member_count

    required_tier = get_required_tier(member_count)
    server_config = get_server_config(guild_id)

    if not server_config:
        await ctx.send("No configuration found for this server. Please ask an admin to set up the server using `!set_role @Role` and `!set_subscription [tier]`.")
        return

    if not server_config.subscription_status:
        await ctx.send("This server does not have an active subscription.")
        return

    subscribed_tier = server_config.tier

    if subscribed_tier not in tier_requirements:
        await ctx.send("Invalid subscription tier configured for this server. Please ask an admin to correct it using `!set_subscription [tier]`.")
        return

    if tier_requirements[subscribed_tier] < tier_requirements[required_tier]:
        await ctx.send(f"This server's subscription ({subscribed_tier}) does not cover {member_count} members. Please upgrade to {required_tier}.")
        return

    if is_user_in_cooldown(ctx.author.id):
        await ctx.send(f"You are currently in a cooldown period. Please wait before attempting to verify again.")
        return

    # Get the user's locale
    # user_locale = str(ctx.author.locale) if ctx.author.locale else 'en-US'
    user_locale = 'en-US' # TODO: Temp Solution
    country_code = locale_to_country_code(user_locale)

    if country_code not in SUPPORTED_COUNTRIES:
        await ctx.send(f"Sorry, the verification bot is not available in your country ({country_code}). Verification cannot proceed.")
        return

    verification_url = await generate_onfido_verification_url(guild_id, ctx.author.id, server_config.role_id, user_locale)

    if not verification_url:
        await ctx.send("Failed to initiate verification process. Please try again later or contact support.")
        return

    track_verification_attempt(ctx.author.id)
    track_command_usage(guild_id, ctx.author.id, "reverify")
    await ctx.send(f"This server has {member_count} members. Click the link below to verify your age: {verification_url}")


@bot.command()
async def set_role(ctx, role: discord.Role):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You do not have permission to use this command.")
        return

    guild_id = str(ctx.guild.id)
    server_config = get_server_config(guild_id)

    if not server_config:
        db_session.execute(servers.insert().values(
            server_id=guild_id,
            owner_id=str(ctx.guild.owner_id),
            role_id=str(role.id),
            subscription_status=True  # Assuming subscription is active for testing
        ))
        db_session.commit()
    else:
        server_config.role_id = str(role.id)
        db_session.commit()

    await ctx.send(f"Role for verification set to: {role.name}")

@bot.command()
async def set_subscription(ctx, tier: str):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You do not have permission to use this command.")
        return

    guild_id = str(ctx.guild.id)
    server_config = get_server_config(guild_id)

    if not server_config:
        await ctx.send("This server does not have an active subscription.")
        return

    if tier not in tier_requirements:
        await ctx.send(f"Invalid tier. Available tiers: {', '.join(tier_requirements.keys())}")
        return

    server_config.tier = tier
    db_session.commit()

    await ctx.send(f"Subscription tier set to: {tier}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

@bot.event
async def on_ready():
    logging.info(f'Bot is ready. Logged in as {bot.user}')

@app.route('/callback', methods=['POST'])
def callback():
    payload = request.json
    if payload['payload']['resource_type'] == 'check' and payload['payload']['action'] == 'completed':
        check_id = payload['payload']['object']['id']
        check_result = payload['payload']['object']['result']
        if check_result == 'clear':
            applicant_id = payload['payload']['object']['applicant_id']
            guild_id, user_id, role_id = applicant_id.split('-')
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(assign_role(guild_id, user_id, role_id))
            update_user_verification_status(user_id, True)
            return "Verification successful, role assigned!"
    return "Verification failed."

@app.route('/start_verification')
def start_verification():
    guild_id = request.args.get('guild_id')
    user_id = request.args.get('user_id')
    role_id = request.args.get('role_id')
    session['guild_id'] = guild_id
    session['user_id'] = user_id
    session['role_id'] = role_id

    authorization_url = (
        f'https://api.us.onfido.com/v3.6/applicants?client_id={ONFIDO_API_TOKEN}&redirect_uri={REDIRECT_URI}&scope=openid'
    )
    return redirect(authorization_url)

@app.route('/analytics')
def analytics():
    result = db_session.query(command_usage).all()
    analytics_data = [{"server_id": row.server_id, "user_id": row.user_id, "command": row.command, "timestamp": row.timestamp} for row in result]
    return jsonify(analytics_data)

def run_flask():
    app.run(port=5000)

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    asyncio.run(bot.start(DISCORD_BOT_TOKEN))