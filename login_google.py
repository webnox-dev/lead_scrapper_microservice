import asyncio
import os
from playwright.async_api import async_playwright

async def main():
    user_data_dir = "./user_profile"
    os.makedirs(user_data_dir, exist_ok=True)
    
    print("=" * 60)
    print("GOOGLE MAPS SESSION LOGIN UTILITY")
    print("=" * 60)
    print(f"Opening browser using profile folder: {os.path.abspath(user_data_dir)}")
    print("Instructions:")
    print("1. A browser window will open in front of you.")
    print("2. Click the 'Sign in' button at the top-right corner of Google Maps.")
    print("3. Log in to your Google Account.")
    print("4. Once you are logged in, close the browser window to save the session.")
    print("=" * 60)
    
    async with async_playwright() as p:
        # Use system Chrome if available, or fallback to default
        import shutil
        chrome_path = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
        
        launch_args = {
            "user_data_dir": user_data_dir,
            "headless": False,  # Headful mode is required for manual login
            "viewport": {"width": 1280, "height": 800},
            "args": [
                "--disable-gpu",
                "--no-sandbox",
            ]
        }
        if chrome_path:
            launch_args["executable_path"] = chrome_path
            
        context = await p.chromium.launch_persistent_context(**launch_args)
        page = await context.new_page()
        
        # Navigate to Google Maps
        await page.goto("https://www.google.com/maps", wait_until="domcontentloaded")
        
        # Poll to keep process alive until the browser window is closed
        while True:
            try:
                # If all pages/windows are closed, exit the loop
                if not context.pages or len(context.pages) == 0:
                    break
                await asyncio.sleep(1.0)
            except Exception:
                break
                
        print("\nSession saved successfully! Your Google Account session is now stored in ./user_profile.")

if __name__ == "__main__":
    asyncio.run(main())
