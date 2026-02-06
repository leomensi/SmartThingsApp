import psycopg2
import pandas as pd
import os
from datetime import datetime

# --- Configuration ---
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "user": os.getenv("DB_USER", "myuser"),
    "password": os.getenv("DB_PASSWORD", "mypassword"),
    "database": os.getenv("DB_NAME", "mydatabase"),
}

def export_db_to_csv():
    # 1. Define the filename with today's date
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename = f"ac_monitoring_export_{timestamp}.csv"

    try:
        # 2. Connect to the database
        conn = psycopg2.connect(**DB_CONFIG)
        
        # 3. Use Pandas to read the SQL table
        print(f"Reading data from table 'ac_monitoring'...")
        query = "SELECT * FROM ac_monitoring ORDER BY timestamp DESC"
        df = pd.read_sql_query(query, conn)

        # 4. Save to CSV
        df.to_csv(filename, index=False, encoding='utf-8')
        
        print(f"✅ Success! Data exported to: {filename}")
        print(f"Total rows exported: {len(df)}")

    except Exception as e:
        print(f"❌ Error during export: {e}")
    
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    export_db_to_csv()