# Newsletter Generator

A web-based newsletter and eblast generator for tactical equipment companies.

## Features

- Create and edit newsletters with multiple section types
- Generate eblasts for promotions
- Export HTML files for email and web use
- Support for multiple brands (Aardvark Tactical, Project 7 Armor)
- Image support for all sections
- Team collaboration with simple password authentication

## Quick Deploy to Render

1. **Fork/Upload this repository** to your GitHub account

2. **Connect to Render:**
   - Go to [render.com](https://render.com)
   - Click "New" → "Web Service"
   - Connect your GitHub repository
   - Render will detect the `render.yaml` file automatically

3. **Set Environment Variables:**
   - `APP_PASSWORD`: Set a secure password for your team
   - Other variables are auto-configured

4. **Deploy:**
   - Click "Deploy" and wait for deployment
   - Your app will be available at `https://your-app-name.onrender.com`

## Manual Deployment

1. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set Environment Variables:**
   ```bash
   export APP_PASSWORD=your-secure-password
   export SESSION_SECRET_KEY=your-secret-key
   ```

3. **Run the Application:**
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```

4. **Access the App:**
   - Open `http://localhost:8000`
   - Login with your APP_PASSWORD

## Usage

1. **Login** with your password
2. **Create Newsletter** or **Eblast**
3. **Edit Sections** - Add content, images, CTAs
4. **Preview** your content
5. **Export** HTML files for use

## Team Access

Share your deployed URL with team members along with the APP_PASSWORD.

## Database

Uses SQLite with persistent storage. Your newsletters and data are automatically saved.

## Support

- Check `/debug/db` endpoint for database status
- Check `/health` for application status
