### Option 1: Windows System Trust Store (Recommended)
1. Double-click certs/ca.crt in File Explorer
2. Click "Install Certificate"
3. Select "Local Machine" → Next
4. Choose "Place all certificates in the following store"
5. Browse to and select "Trusted Root Certification Authorities"
6. Finish → OK

### Option 2: Chrome/Edge Only
1. Open chrome://settings/certificates (or edge://settings/certificates)
2. Go to "Authorities" tab
3. Click "Import..." → Select certs/ca.crt
4. Check "Trust this certificate for identifying websites"

### After Installation
1. Restart your browser completely (close all windows)
2. Restart the client UI with CLIENT\ZZZ_CLIENT.bat (and the SERVER with ZZZ_SERVER.bat if it is running)
3. Navigate to https://127.0.0.1:8000/
4. You should now see 🔒 "Secure" instead of "Not secure"