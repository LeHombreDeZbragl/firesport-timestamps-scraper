import requests
import os
import sys
from pathlib import Path

def download_league_data(league_name, start_year, end_year):
    """
    Download HTML files for a league across multiple years.
    
    Args:
        league_name: League identifier (e.g., 'vcbl', 'zl')
        start_year: First year to download
        end_year: Last year to download (inclusive)
    """
    # Create league folder if it doesn't exist
    league_folder = Path(league_name.lower())
    league_folder.mkdir(exist_ok=True)
    
    # Download HTML for each year
    for year in range(start_year, end_year + 1):
        url = f"https://{league_name.lower()}.firesport.eu/web_souteze.php?akce=sel&rok={year}"
        
        try:
            html = requests.get(url).text
            output_file = league_folder / f"{league_name.upper()}.{year}.html"
            
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(html)
            
            print(f"Saved {output_file}")
        except Exception as e:
            print(f"Error downloading {year}: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python downloader.py <league> <start_year> <end_year>")
        print("Example: python downloader.py vcbl 2022 2025")
        sys.exit(1)
    
    league = sys.argv[1]
    start_year = int(sys.argv[2])
    end_year = int(sys.argv[3])
    
    download_league_data(league, start_year, end_year)