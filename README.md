# email-automation-agent

Sheet (leads) -> Gmail (send + follow-up) -> reply/bounce detect -> AI intent -> Sheet update + Telegram alert.

## Setup
1. `pip install -r requirements.txt`
2. Google Cloud: enable Gmail API + Sheets API. OAuth client (Desktop) -> save as `credentials.json`. Set consent screen to **Production** (Testing = token dies in 7 days).
3. Edit `.env`: SHEET_ID, SENDER_NAME, SENDER_ADDRESS, OFFER_LINE (+ Telegram, GEMINI_API_KEY optional).
4. `python main.py auth` (PC with browser; copy token.json to server if headless)
5. `python main.py doctor`
6. `DRY_RUN=true python main.py outreach --force` -> check logs, then set DRY_RUN=false
7. Send first 3-5 mails to your own address (put your emails in the sheet), then real leads.
8. `python main.py loop` (or cron: `*/20 * * * * python main.py all`)

## Sheet
Needs `email` column (also reads `Owner Email`, `Owner Name`, `Agency`, `Personalization Hook`). Missing columns are added automatically.
Status: new -> sent -> followup1 -> followup2 -> no_reply | replied | not_interested | unsubscribed | bounced | invalid | skipped

## Test
`python -m unittest discover -s tests -v`
