import os
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv('/home/stellaradmin/my_app/keys.env')

sender = os.getenv("EMAIL_USER")
password = os.getenv("EMAIL_PASS")

if not sender or not password:
    print("Error: EMAIL_USER or EMAIL_PASS not found.")
    exit(1)

msg = EmailMessage()
msg['Subject'] = "Automated Notice: Stellar Platform Access Revoked"
msg['From'] = f"Stellar AI <{sender}>"
msg['To'] = "kgcartoon07@gmail.com"

body = """Dear Kg Cartoon,

This is an automated notification from the Stellar Autonomous Environment.

Your access to the Stellar platform has been revoked, and your account has been placed back on the waitlist. 

This action was triggered automatically by our system monitoring protocols due to repeated submissions of project requests that exceed platform infrastructure capabilities and violate our operational constraints. Specifically, persistent requests for local AI video generation and associated large-scale infrastructure—without requisite provisioning or training data—have flagged your account for unrealistic resource demands.

Our platform enforces strict resource and architectural boundaries. To maintain service stability for all users, accounts that continually submit ungrounded, infrastructure-intensive requests are automatically suspended.

All of your active background processes have been terminated. 

If you believe this action was taken in error, you may reply to this email, though restoration of access is not guaranteed.

System Administrator
Stellar Autonomous Environment
"""

msg.set_content(body)

try:
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print("Email sent successfully.")
except Exception as e:
    print(f"Error sending email: {e}")
