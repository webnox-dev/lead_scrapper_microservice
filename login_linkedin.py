import asyncio
import os
from playwright.async_api import async_playwright

async def main():
    user_data_dir = "./user_profile"
    os.makedirs(user_data_dir, exist_ok=True)
    
    print("=" * 60)
    print("LINKEDIN SESSION LOGIN UTILITY")
    print("=" * 60)
    print(f"Opening browser using profile folder: {os.path.abspath(user_data_dir)}")
    print("Instructions:")
    print("1. A browser window will open shortly.")
    print("2. Log in using your LinkedIn account:")
    print("   Email: naveen2k321@gmail.com")
    print("   Password: your_password (e.g. naveen2k@)")
    print("3. If LinkedIn prompts you to approve via your mobile app, do so.")
    print("4. Once you are successfully logged in and see the LinkedIn feed,")
    print("   close the browser window to save the session.")
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
                "--disable-blink-features=AutomationControlled",
            ]
        }
        if chrome_path:
            launch_args["executable_path"] = chrome_path
            
        context = await p.chromium.launch_persistent_context(**launch_args)
        page = await context.new_page()
        
        # Navigate to LinkedIn
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        
        # Poll to keep process alive until the browser window is closed
        while True:
            try:
                # If all pages/windows are closed, exit the loop
                if not context.pages or len(context.pages) == 0:
                    break
                await asyncio.sleep(1.0)
            except Exception:
                break
                
        print("\nSession saved successfully! Your LinkedIn session is now stored in ./user_profile.")

if __name__ == "__main__":
    asyncio.run(main())
