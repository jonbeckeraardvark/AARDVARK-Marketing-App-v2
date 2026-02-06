# 🚀 Newsletter Generator - Complete Deployment Guide

## 📁 What's in This Package

Your complete newsletter generator includes:

```
newsletter-generator/
├── app.py                    # Main application (with database fixes)
├── requirements.txt          # Python dependencies
├── render.yaml              # Deployment configuration
├── .gitignore               # Git security settings
├── README.md                # Documentation
├── DEPLOYMENT.md            # This guide
└── templates/               # Web page templates
    ├── home.html            # Dashboard page
    ├── login.html           # Login page
    ├── editor.html          # Newsletter editor
    └── eblast_editor.html   # Eblast editor
```

## 🎯 Quick Deployment Steps

### STEP 1: Upload to GitHub
1. **Extract this zip** to your Git-connected folder
2. **Open GitHub Desktop**
3. **Commit all files**: `Initial newsletter generator upload`
4. **Push to GitHub**

### STEP 2: Deploy on Render
1. **Go to [render.com](https://render.com)**
2. **New + → Web Service**
3. **Connect your GitHub repository**
4. **Render auto-configures** from `render.yaml`
5. **Set your password** in APP_PASSWORD environment variable
6. **Deploy!**

### STEP 3: Test Your App
1. **Visit your Render URL**
2. **Login with your password**
3. **Create a test newsletter**
4. **Share URL + password with team**

## ⚙️ Environment Variables to Set in Render

| Variable | Value | Notes |
|----------|-------|-------|
| `APP_PASSWORD` | `YourSecurePassword123` | Change this! |
| `SESSION_SECRET_KEY` | (auto-generated) | Leave as is |
| `DB_PATH` | `/var/data/newsletters.db` | Auto-configured |

## ✅ Features Included

- ✅ **Create & edit newsletters** with multiple sections
- ✅ **Create & edit eblasts** for promotions
- ✅ **Image support** for all sections
- ✅ **Multiple brands** (Aardvark Tactical, Project7 Armor)
- ✅ **HTML export** for email and web use
- ✅ **Database persistence** (no data loss)
- ✅ **Team collaboration** with password protection
- ✅ **Multiple events** support in newsletters
- ✅ **No "read more" truncation** in emails
- ✅ **No footer** in website versions

## 🛠️ Technical Notes

### Database
- **Location**: `/var/data/newsletters.db` (persistent disk)
- **Type**: SQLite (reliable, no external dependencies)
- **Backup**: Visit `/debug/backup` to create backups

### Security
- **Password protection**: Single shared password for team access
- **No sensitive data** in code repository
- **Environment variables** store secrets securely

### Monitoring
- **Health check**: `/health`
- **Database status**: `/debug/db`
- **Backup database**: `/debug/backup`

## 🚨 If Something Goes Wrong

### Build Fails
1. **Check Render logs** for error messages
2. **Verify all files** uploaded to GitHub
3. **Ensure `templates/` folder** contains all 4 HTML files

### Database Issues
1. **Visit `/debug/db`** to check status
2. **Should show database at** `/var/data/newsletters.db`
3. **If newsletters disappearing**, check persistent disk configuration

### Login Issues
1. **Verify APP_PASSWORD** is set in Render environment variables
2. **Try hard refresh** (Ctrl+F5)
3. **Check browser console** for errors

## 💰 Cost Estimate

- **Render Free Tier**: $0/month (spins down after inactivity)
- **Render Starter**: $7/month (always on, better performance)

## 🎉 Success Indicators

Your deployment is successful when:
- ✅ **App loads** at your Render URL
- ✅ **Login works** with your password
- ✅ **Can create newsletters** and eblasts
- ✅ **Database persists** between visits
- ✅ **Export works** (HTML download)
- ✅ **Team can access** with shared URL + password

## 📞 Need Help?

- **Check Render logs** for detailed error messages
- **Visit monitoring URLs**: `/health`, `/debug/db`
- **Verify environment variables** are set correctly
- **Ensure persistent disk** is configured (1GB at `/var/data`)

Your newsletter generator is now ready for professional use by your team!
