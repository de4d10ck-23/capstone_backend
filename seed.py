import asyncio
from core.database import get_supabase
from core.security import hash_password

async def seed_admin():
    """Seed the default administrator account."""
    print("Seeding default admin account...")
    sb = get_supabase()
    
    username = "admin12345"
    password = "admin12345"
    
    # Check if admin already exists
    existing = sb.table("users").select("id").eq("username", username).execute()
    if existing.data:
        print("Admin account already exists. Skipping.")
        return
        
    admin_user = {
        "full_name": "System Administrator",
        "username": username,
        "password_hash": hash_password(password),
        "role": "admin",
        "is_active": True
    }
    
    result = sb.table("users").insert(admin_user).execute()
    if result.data:
        print(f"Admin account created successfully! Username: {username} Password: {password}")
    else:
        print("Failed to create admin account.")

if __name__ == "__main__":
    asyncio.run(seed_admin())
