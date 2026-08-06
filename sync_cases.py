import os
import requests
import logging
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, Date, ForeignKey, DateTime, Table
from sqlalchemy.orm import DeclarativeBase, sessionmaker, relationship
from datetime import datetime

# -----------------------------------------
# CONFIGURE PRODUCTION LOGGING
# -----------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "cases_sync.log")),
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

case_patents_table = Table(
    "case_patents",
    Base.metadata,
    Column("case_id", Integer, ForeignKey("cases.id"), primary_key=True),
    Column("patent_id", Integer, ForeignKey("patents.id"), primary_key=True),
)

class Patent(Base):
    __tablename__ = "patents"
    id = Column(Integer, primary_key=True)
    patent_number = Column(Text, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    cases = relationship("Case", secondary=case_patents_table, back_populates="patents")

class Case(Base):
    __tablename__ = "cases"
    id = Column(Integer, primary_key=True)
    case_number = Column(Text, unique=True, nullable=False)
    case_type = Column(String(20), nullable=False)
    jurisdiction = Column(Text)
    caption = Column(Text)
    filed_date = Column(Date)
    status = Column(String(50))
    outcome = Column(Text)
    last_event = Column(Text)
    last_event_date = Column(Date)
    patents_numbers = Column(Text)
    patents_g_numbers = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    patents = relationship("Patent", secondary=case_patents_table, back_populates="cases")

engine = create_engine(DB_URL)
SessionLocal = sessionmaker(bind=engine)

# -----------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------
def parse_date(date_str):
    if not date_str:
        return None
    try:
        # e.g., "2024-05-12T00:00:00" -> "2024-05-12"
        return datetime.strptime(str(date_str).split("T")[0], "%Y-%m-%d").date()
    except:
        return None

# -----------------------------------------
# MAIN SYNC LOGIC
# -----------------------------------------
def sync_cases(test_mode=True):
    logger.info("========================================")
    logger.info("    STARTING EXPARTE CASE CRON SYNC     ")
    logger.info("========================================")
    
    session = SessionLocal()
    url = "https://api.exparte.com/search/cases"
    headers = {
        "authorization": f"Bearer {TOKEN}",
        "content-type": "application/json",
        "origin": "https://ai-lab.exparte.com"
    }

    limit = 100
    case_types = ["DCT", "PTAB", "CAFC", "ITC"]

    total_new_inserts = 0
    total_updates = 0

    for c_type in case_types:
        logger.info(f"\n--- Syncing {c_type} Cases ---")
        page = 1
        
        while True:
            logger.info(f"-> Fetching {c_type} Page {page}...")
            payload = {
                "caseType": c_type, 
                "caseNumber": "",
                "agent": "",
                "keyEventsLogical": "or",
                "partiesLogical": "or",
                "lawFirmsLogical": "or",
                "lawyersLogical": "or",
                "orderBy": "filed",
                "orderDirection": "DESC",
                "limit": limit,
                "page": page
            }
            
            # Exponential backoff retry logic
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
                    backoff_time = 2 ** attempt
                    logger.warning(f"   [!] API Error {resp.status_code} on attempt {attempt+1}. Retrying in {backoff_time}s...")
                    import time
                    time.sleep(backoff_time)
                    
            if not success:
                logger.error(f"Failed to fetch {c_type} Page {page} after {max_retries} attempts. Stopping {c_type}.")
                break
                
            if not data:
                logger.info(f"No more data for {c_type}.")
                break

            page_inserts = 0
            page_updates = 0
            
            for item in data:
                case_num = item.get("CaseNumber")
                if not case_num:
                    continue
                    
                db_case = session.query(Case).filter_by(case_number=case_num).first()
                
                if db_case:
                    # Update dynamic stats
                    db_case.status = item.get("Status")
                    db_case.outcome = item.get("Outcome")
                    db_case.last_event = item.get("LastEvent")
                    db_case.last_event_date = parse_date(item.get("LastEventDate"))
                    page_updates += 1
                else:
                    # Insert new case
                    db_case = Case(
                        case_number=case_num,
                        case_type=c_type,
                        jurisdiction="US",
                        caption=item.get("Caption"),
                        filed_date=parse_date(item.get("Filed")),
                        status=item.get("Status"),
                        outcome=item.get("Outcome"),
                        last_event=item.get("LastEvent"),
                        last_event_date=parse_date(item.get("LastEventDate")),
                        patents_numbers=item.get("PatentsNumbers"),
                        patents_g_numbers=item.get("PatentsGNumbers")
                    )
                    
                    session.add(db_case)
                    
                    # Link Patents
                    p_nums_raw = item.get("PatentsNumbers")
                    if p_nums_raw:
                        p_list = list(set([p.strip() for p in str(p_nums_raw).split(",") if p.strip()]))
                        for p_num in p_list:
                            # Check if patent exists
                            db_patent = session.query(Patent).filter_by(patent_number=p_num).first()
                            if not db_patent:
                                db_patent = Patent(patent_number=p_num)
                                session.add(db_patent)
                            db_case.patents.append(db_patent)
                    
                    page_inserts += 1

            session.commit()
            total_new_inserts += page_inserts
            total_updates += page_updates
            logger.info(f"   [OK] Page {page} saved. (Inserted: {page_inserts}, Updated: {page_updates})")
            
            # EARLY STOP LOGIC
            # If the entire page was just updates (0 new cases), we have caught up to our historical data!
            if page_inserts == 0 and page > 1:
                logger.info(f"   [EARLY STOP] No new {c_type} cases found on this page. Database is up to date!")
                break
                
            # TESTING STOP
            if test_mode and page >= 2:
                logger.info(f"   [TEST MODE] Stopping {c_type} after 2 pages.")
                break
                
            page += 1

    session.close()
    logger.info("\n========================================")
    logger.info(f" FINAL SYNC: {total_new_inserts} new | {total_updates} updated")
    logger.info("========================================")

if __name__ == "__main__":
    sync_cases(test_mode=False)
