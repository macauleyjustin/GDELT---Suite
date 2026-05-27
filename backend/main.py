from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import bigquery
import hashlib
import os
import uvicorn
import random
import requests
import time
import json
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# BigQuery Cost Guard
safe_config = bigquery.QueryJobConfig(maximum_bytes_billed=5 * 1024 * 1024 * 1024) # 5 GB strict limit

# Cache for 5 minutes
QUERY_CACHE = {}
CACHE_TTL = 300 

client = None

def get_bq_client():
    global client
    if client: return client
    try:
        client = bigquery.Client(project="gdelt-497507")
        return client
    except Exception as e:
        return None

class Deduplicator:
    def __init__(self):
        self.seen_hashes = set()

    def is_duplicate(self, text: str) -> bool:
        if not text:
            return True
        h = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if h in self.seen_hashes:
            return True
        self.seen_hashes.add(h)
        if len(self.seen_hashes) > 100000:
            self.seen_hashes.clear()
        return False

dedup = Deduplicator()

def extract_primary_location(v2locations: str, target_city: str = None):
    """Parses GDELT V2Locations and extracts the best coordinate, optionally matching a target city."""
    if not v2locations:
        return None
    
    locations = v2locations.split(';')
    
    # If target_city is provided, scan the entire array for it first
    if target_city:
        target_lower = target_city.lower()
        for loc in locations:
            parts = loc.split('#')
            if len(parts) >= 7 and target_lower in parts[1].lower():
                try:
                    lat = float(parts[5])
                    lon = float(parts[6])
                    if lat != 0.0 or lon != 0.0:
                        return {"name": parts[1], "lat": lat, "lon": lon}
                except ValueError:
                    continue
        # Strict mode for local scope: DO NOT fallback to random state/country centers.
        return None

    # Fallback to the first valid coordinate if NO target_city was provided (Global scope)
    for loc in locations:
        parts = loc.split('#')
        if len(parts) >= 7:
            loc_type = parts[0]
            # Prefer city/landmark level types (3, 4, 5) or fallback to others if valid coords
            try:
                lat = float(parts[5])
                lon = float(parts[6])
                if lat != 0.0 or lon != 0.0:
                    return {
                        "name": parts[1],
                        "lat": lat,
                        "lon": lon
                    }
            except ValueError:
                continue
    return None

@app.get("/api/pulse")
async def get_global_pulse(
    limit: int = 2000, 
    theme: str = None, 
    emotion: str = None, 
    scope: str = 'global', 
    city: str = None,
    days: int = 1,
    domain: str = None,
    min_tone: float = None,
    max_tone: float = None
):
    bq = get_bq_client()
    if not bq:
        raise HTTPException(status_code=500, detail="BigQuery client not configured. You MUST run 'gcloud auth application-default login' in your terminal.")
    
    # Enforce a max of 3 days to prevent blowing past quotas
    days = min(max(1, days), 3)
    
    # Generate Cache Key
    cache_key = f"{limit}_{theme}_{emotion}_{scope}_{city}_{days}_{domain}"
    if cache_key in QUERY_CACHE:
        cached_data, timestamp = QUERY_CACHE[cache_key]
        if time.time() - timestamp < CACHE_TTL:
            return {"status": "ok", "data": cached_data, "cached": True}
    
    # Build dynamic WHERE clauses
    where_clauses = [f"DATE(_PARTITIONTIME) >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)", "V2Locations IS NOT NULL", "V2Tone IS NOT NULL"]
    
    if theme:
        # Broaden the search to make custom terms much more effective
        term = theme.lower().replace("'", "''")
        where_clauses.append(f"(LOWER(V2Themes) LIKE '%{term}%' OR LOWER(V2Persons) LIKE '%{term}%' OR LOWER(V2Organizations) LIKE '%{term}%')")
    
    if emotion:
        emotion = emotion.lower()
        if emotion == 'sadness':
            where_clauses.append(r"REGEXP_CONTAINS(LOWER(V2Themes), r'(sad|mourn|tragedy|grief)')")
        elif emotion == 'fear' or emotion == 'panic':
            where_clauses.append(r"REGEXP_CONTAINS(LOWER(V2Themes), r'(fear|terror|panic)')")
        elif emotion == 'happiness':
            where_clauses.append(r"REGEXP_CONTAINS(LOWER(V2Themes), r'(happy|joy|celebrat)')")
        elif emotion == 'anger':
            where_clauses.append(r"REGEXP_CONTAINS(LOWER(V2Themes), r'(anger|hostil|rage|protest)')")
        else:
            where_clauses.append(f"LOWER(V2Themes) LIKE '%{emotion}%'")
            
    if domain:
        where_clauses.append(f"LOWER(DocumentIdentifier) LIKE '%{domain.lower()}%'")
        
    if scope == 'local' and city:
        where_clauses.append(f"LOWER(V2Locations) LIKE '%{city.lower()}%'")
        
    if min_tone is not None:
        where_clauses.append(f"CAST(SPLIT(V2Tone, ',')[OFFSET(0)] AS FLOAT64) >= {min_tone}")
        
    if max_tone is not None:
        where_clauses.append(f"CAST(SPLIT(V2Tone, ',')[OFFSET(0)] AS FLOAT64) <= {max_tone}")
        
    where_sql = " AND ".join(where_clauses)
        
    table_clause = "`gdelt-bq.gdeltv2.gkg_partitioned`"
    if scope == 'global':
        table_clause += " TABLESAMPLE SYSTEM (5 PERCENT)"
        
    query = f"""
    SELECT 
        V2Locations,
        V2Tone,
        V2Themes,
        V2Persons,
        V2Organizations,
        DocumentIdentifier,
        DATE(_PARTITIONTIME) as date_added
    FROM {table_clause}
    WHERE {where_sql}
    LIMIT {limit}
    """
    
    try:
        query_job = bq.query(query, job_config=safe_config)
        results = query_job.result()
        
        pulse_data = []
        for row in results:
            doc_id = row.DocumentIdentifier
            if dedup.is_duplicate(doc_id):
                continue
                
            tone_str = row.V2Tone
            tone_parts = tone_str.split(',') if tone_str else []
            
            geo = extract_primary_location(row.V2Locations, city if scope == 'local' else None)
            
            # Phase 5: Deep Semantic Emotion Analysis
            themes_str = row.V2Themes or ""
            themes_lower = themes_str.lower()
            
            # GDELT V2Tone: 0=Tone, 1=Pos, 2=Neg, 3=Polarity, 4=Activity, 5=Self/Group, 6=WordCount
            try:
                tone = float(tone_parts[0])
                pos = float(tone_parts[1]) if len(tone_parts) > 1 else 0
                neg = float(tone_parts[2]) if len(tone_parts) > 2 else 0
                polarity = float(tone_parts[3]) if len(tone_parts) > 3 else 0
                activity = float(tone_parts[4]) if len(tone_parts) > 4 else 0
                anger_score = (neg * 0.6) + (activity * 0.4) if tone < -2 and activity > 2 else 0
                anxiety = (neg * 0.5) + (activity * 0.5) if tone < -4 else 0
            except ValueError:
                tone, pos, neg, polarity, activity, anger_score, anxiety = 0, 0, 0, 0, 0, 0, 0

            # Base scores off lexical tone
            anger_val = (neg * 0.6) + (activity * 0.4) if tone < -2 and activity > 2 else 0
            sadness_val = (neg * 0.8) if tone < -1 and activity <= 2 else 0
            fear_val = (neg * 0.5) + (activity * 0.5) if tone < -4 else 0
            happiness_val = (pos * 1.5) if tone > 2 else 0
            neutral_val = 5.0 if (-2 <= tone <= 2) else 0

            # Boost with semantic themes
            anger_val += len(re.findall(r'(anger|hostil|rage|protest|riot|violenc|rebel)', themes_lower)) * 10
            sadness_val += len(re.findall(r'(sad|mourn|tragedy|grief|disaster|death|casualt)', themes_lower)) * 10
            fear_val += len(re.findall(r'(fear|terror|panic|threat|warn|alert|crisis|wmd)', themes_lower)) * 10
            happiness_val += len(re.findall(r'(happy|joy|celebrat|award|peace|truce|success)', themes_lower)) * 10
            
            emotion_scores = {"anger": anger_val, "sadness": sadness_val, "fear": fear_val, "happiness": happiness_val, "neutral": neutral_val}
            dominant_emotion = "neutral"
            max_score = -1
            for emo, score in emotion_scores.items():
                if score > max_score:
                    dominant_emotion = emo
                    max_score = score
                    
            # Extract top 3 themes for analytics
            theme_list = [t.split(',')[0] for t in themes_str.split(';') if t]
            valid_themes = [t for t in theme_list if t and not t.startswith('AFFECT') and not t.startswith('TAX_')][:3]

            # Extract OSINT Entities
            persons_str = row.V2Persons or ""
            orgs_str = row.V2Organizations or ""
            
            persons_list = list(dict.fromkeys([p.split(',')[0] for p in persons_str.split(';') if p]))[:5]
            orgs_list = list(dict.fromkeys([o.split(',')[0] for o in orgs_str.split(';') if o]))[:5]
            
            if geo:
                pulse_data.append({
                    "id": doc_id,
                    "url": doc_id,
                    "location": geo,
                    "themes": valid_themes,
                    "persons": persons_list,
                    "organizations": orgs_list,
                    "dominant_emotion": dominant_emotion,
                    "sentiment": {
                        "polarity": polarity,
                        "anxiety": anxiety,
                        "anger": anger_score
                    },
                    "date": str(row.date_added) if row.date_added else "Unknown"
                })
        
        # Save to cache
        QUERY_CACHE[cache_key] = (pulse_data, time.time())
        return {"status": "ok", "data": pulse_data, "cached": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
