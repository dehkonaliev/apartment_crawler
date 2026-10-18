Tashkent apartment rental finder (Telegram) - 100% FREE version.

No AI and no paid API. Price and rooms are read from the ad text with rules.

Pipeline:
  1. Read recent messages from your channel list (your account does this).
  2. Word filters: must be a rent ad, must match location and gender.
     Sale ads, daily rent ads and "I am looking for a flat" ads are removed.
  3. Remove duplicate ads (same text posted many times).
  4. Read PRICE and ROOMS from the text (handles $, y.e., so'm, mln, ming, 2 xonali, 2-комн...).
  5. Filter by price and rooms, sort by price, send results.

Commands (only the owner can use them):
  /rent loc=chilonzor,yunusobod price=150-300 rooms=2 gender=girls days=5
  /check <paste an ad>         (shows what the bot understands from one ad)
  /search some text            (search all chats and public channels)
  /find @channel some text     (search inside one channel)

Go to my.telegram.org and create one app and get API_ID and API_HASH

.env file:
  API_ID=1234567
  API_HASH=your_api_hash
  BOT_TOKEN=123456:ABC...
  OWNER_ID=your_telegram_user_id
  CHANNELS=@channel_one,@channel_two,https://t.me/channel_three
  USD_UZS=12500                (optional: so'm per 1 dollar)

Install:  uv add telethon python-dotenv

Example "CHANNELS=" you might need in searching apartments from tashkent
CHANNELS=@Studentlarga_kvartira_bezmakler,@maklersiz_ijaragai @chilonzorda_ijaraga_kvartira
