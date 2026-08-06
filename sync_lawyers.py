import os
import requests
import json
import logging
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime

# -----------------------------------------
# CONFIGURE PRODUCTION LOGGING
# -----------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "lawyers_sync.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load Environment Variables
load_dotenv()
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("EXPARTE_TOKEN")

if not DB_URL or not TOKEN:
    logger.error("Missing DATABASE_URL or EXPARTE_TOKEN in .env")
    exit(1)

# -----------------------------------------
# DATABASE MODELS
# -----------------------------------------
class Base(DeclarativeBase):
    pass

class LawFirm(Base):
    __tablename__ = "law_firms"
    id = Column(Integer, primary_key=True)
    exparte_firm_id = Column(Integer, unique=True, nullable=True)
    name = Column(Text, nullable=False)
    firm_type = Column(String(50))
    jurisdiction = Column(String(10), default="US")

class Attorney(Base):
    __tablename__ = "attorneys"
    id = Column(Integer, primary_key=True)
    exparte_lawyer_id = Column(Integer, unique=True, nullable=True)
    name = Column(Text, nullable=False)
    law_firm_id = Column(Integer, ForeignKey("law_firms.id"), nullable=True)
    jurisdiction = Column(String(10), default="US")
    experience = Column(Integer, default=0)
    expertise = Column(JSONB)
    firm_type = Column(String(50))
    cafc_experience = Column(Integer)
    cafc_rating = Column(String(10))
    cafc_trend = Column(String(10))
    dct_experience = Column(Integer)
    dct_rating = Column(String(10))
    dct_trend = Column(String(10))
    ptab_experience = Column(Integer)
    ptab_rating = Column(String(10))
    ptab_trend = Column(String(10))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

engine = create_engine(DB_URL)
SessionLocal = sessionmaker(bind=engine)

# -----------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------
def clean_int(val):
    if not val: return 0
    try: return int(float(val))
    except: return 0

def parse_expertise(raw_str):
    if not raw_str or str(raw_str).strip() == "" or raw_str == ": ": return {}
    res = {}
    for part in str(raw_str).split(";"):
        if ":" in part:
            domain, count = part.rsplit(":", 1)
            try: res[domain.strip()] = int(count.strip())
            except: res[domain.strip()] = 0
    return res

# -----------------------------------------
# MAIN SYNC LOGIC
# -----------------------------------------
def sync_lawyers(test_mode=True):
    logger.info("========================================")
    logger.info("   STARTING EXPARTE LAWYER CRON SYNC    ")
    logger.info("========================================")
    
    session = SessionLocal()
    url = "https://api.exparte.com/search/lawyers"
    headers = {
        "authorization": f"Bearer {TOKEN}",
        "content-type": "application/json",
        "origin": "https://ai-lab.exparte.com"
    }

    page = 1
    limit = 100
    
    new_inserts = 0
    updates = 0

    while True:
        logger.info(f"-> Fetching Page {page}...")
        payload = {
            "name": "",
            "lawFirmName": [],
            "orderBy": "lawyerName",
            "orderDirection": "ASC",
            "limit": limit,
            "page": page
        }
        
        # Exponential backoff retry logic for Exparte API hiccups (500 errors)
        max_retries = 5
        success = False
        data = []
        
        for attempt in range(max_retries):
            resp = requests.post(url, json=payload, headers=headers)
            if resp.status_code in [200, 201]:
                data = resp.json().get("items", [])
                success = True
                break
            else:
                backoff_time = 2 ** attempt # 1s, 2s, 4s, 8s, 16s
                logger.warning(f"   [!] API Error {resp.status_code} on attempt {attempt+1}. Retrying in {backoff_time}s...")
                import time
                time.sleep(backoff_time)
                
        if not success:
            logger.error(f"Failed to fetch Page {page} after {max_retries} attempts. Stopping.")
            break
            
        if not data:
            logger.info("No more data found. Sync Complete.")
            break
            
        for item in data:
            # 1. UPSERT LAW FIRM
            firm_id = item.get("LawFirmID")
            db_firm_id = None
            if firm_id:
                firm = session.query(LawFirm).filter_by(exparte_firm_id=firm_id).first()
                if not firm:
                    firm = LawFirm(
                        exparte_firm_id=firm_id,
                        name=item.get("LawFirmName", "Unknown"),
                        firm_type=item.get("FirmType"),
                        jurisdiction="US"
                    )
                    session.add(firm)
                    session.flush() # Get the new ID
                db_firm_id = firm.id

            # 2. UPSERT ATTORNEY
            lawyer_id = item.get("LawyerID")
            attorney = session.query(Attorney).filter_by(exparte_lawyer_id=lawyer_id).first()
            
            if attorney:
                # Update existing records to keep stats fresh
                attorney.experience = clean_int(item.get("Experience"))
                attorney.expertise = parse_expertise(item.get("Expertise"))
                attorney.dct_rating = item.get("dctRating")
                attorney.ptab_rating = item.get("ptabRating")
                attorney.cafc_rating = item.get("cafcRating")
                updates += 1
            else:
                # Insert brand new attorney
                attorney = Attorney(
                    exparte_lawyer_id=lawyer_id,
                    name=item.get("LawyerName"),
                    law_firm_id=db_firm_id,
                    jurisdiction="US",
                    experience=clean_int(item.get("Experience")),
                    expertise=parse_expertise(item.get("Expertise")),
                    firm_type=item.get("FirmType"),
                    cafc_experience=clean_int(item.get("cafcExperience")),
                    cafc_rating=item.get("cafcRating"),
                    cafc_trend=item.get("cafcTrend"),
                    dct_experience=clean_int(item.get("dctExperience")),
                    dct_rating=item.get("dctRating"),
                    dct_trend=item.get("dctTrend"),
                    ptab_experience=clean_int(item.get("ptabExperience")),
                    ptab_rating=item.get("ptabRating"),
                    ptab_trend=item.get("ptabTrend")
                )
                session.add(attorney)
                new_inserts += 1

        # Commit at the end of every page to save progress safely
        session.commit()
        logger.info(f"   [OK] Page {page} saved. (Inserted: {new_inserts}, Updated: {updates})")
        
        # EARLY STOP FOR TESTING
        if test_mode and page >= 2:
            logger.info("\n[TEST MODE] Stopping after 2 pages. Set test_mode=False to run entire database.")
            break
            
        page += 1

    session.close()
    logger.info("========================================")
    logger.info(f" SYNC COMPLETE: {new_inserts} new | {updates} updated")
    logger.info("========================================")

if __name__ == "__main__":
    # Change to False to run the full sync
    sync_lawyers(test_mode=False)
