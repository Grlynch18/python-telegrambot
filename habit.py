import logging
import sqlite3
import os
from dotenv import load_dotenv
load_dotenv(dotenv_path="./habit-tracker.env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
print("Loaded tokens successfully")

from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
import google.generativeai as genai

# Configure Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
# Use Gemini 2.0 Flash - Fast, efficient, and free tier
model = genai.GenerativeModel('models/gemini-2.0-flash-exp')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
DB_NAME = 'habit_tracker.db'

def init_db():
    """Initialize the database"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Create habits table
    c.execute('''
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            habit_name TEXT NOT NULL,
            created_date TEXT NOT NULL,
            UNIQUE(user_id, habit_name)
        )
    ''')
    
    # Create completions table
    c.execute('''
        CREATE TABLE IF NOT EXISTS completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL,
            completion_date TEXT NOT NULL,
            FOREIGN KEY (habit_id) REFERENCES habits (id) ON DELETE CASCADE,
            UNIQUE(habit_id, completion_date)
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")

def get_user_habits(user_id):
    """Get all habits for a user"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT id, habit_name, created_date FROM habits WHERE user_id = ?', (user_id,))
    habits = c.fetchall()
    conn.close()
    return habits

def get_habit_completions(habit_id):
    """Get all completion dates for a habit"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT completion_date FROM completions WHERE habit_id = ? ORDER BY completion_date DESC', (habit_id,))
    dates = [row[0] for row in c.fetchall()]
    conn.close()
    return dates

def add_habit_to_db(user_id, habit_name):
    """Add a new habit"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO habits (user_id, habit_name, created_date) VALUES (?, ?, ?)',
            (user_id, habit_name, datetime.now().strftime('%Y-%m-%d'))
        )
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    conn.close()
    return success

def complete_habit_in_db(user_id, habit_name, date):
    """Mark a habit as complete for a date"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Get habit_id
    c.execute('SELECT id FROM habits WHERE user_id = ? AND habit_name = ?', (user_id, habit_name))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return False, "Habit not found"
    
    habit_id = result[0]
    
    try:
        c.execute('INSERT INTO completions (habit_id, completion_date) VALUES (?, ?)', (habit_id, date))
        conn.commit()
        conn.close()
        return True, "Completed"
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Already completed"

def delete_habit_from_db(user_id, habit_name):
    """Delete a habit"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('DELETE FROM habits WHERE user_id = ? AND habit_name = ?', (user_id, habit_name))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def calculate_streak(dates):
    """Calculate current streak from list of completion dates"""
    if not dates:
        return 0
    
    # Sort dates in descending order
    sorted_dates = sorted([datetime.strptime(d, '%Y-%m-%d').date() for d in dates], reverse=True)
    
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    
    # Check if completed today or yesterday
    if sorted_dates[0] not in [today, yesterday]:
        return 0
    
    streak = 0
    current_date = today if sorted_dates[0] == today else yesterday
    
    for date in sorted_dates:
        if date == current_date:
            streak += 1
            current_date -= timedelta(days=1)
        else:
            break
    
    return streak

# AI FUNCTIONS
async def extract_habit_from_text(text):
    """Use AI to extract habit from natural language"""
    prompt = f"""Extract the main habit from this text. Return ONLY the habit name in a clear, concise format (2-5 words max).
    
Examples:
- "I want to start exercising daily" -> "Exercise Daily"
- "help me drink more water" -> "Drink Water"
- "I need to read books" -> "Read Books"

User text: "{text}"

Return only the habit name, nothing else:"""
    
    try:
        generation_config = genai.types.GenerationConfig(
            max_output_tokens=50,
            temperature=0.5,
        )
        response = model.generate_content(prompt, generation_config=generation_config, request_options={'timeout': 10})
        habit_name = response.text.strip().strip('"').strip("'")
        return habit_name if habit_name else None
    except Exception as e:
        logger.error(f"AI extraction error: {e}")
        return None

async def generate_motivation(habit_name, streak, total_completions):
    """Generate personalized motivational message"""
    prompt = f"""Generate a SHORT, encouraging message (1-2 sentences max) for someone who just completed their habit.
    
Habit: {habit_name}
Current Streak: {streak} days
Total Completions: {total_completions}

Make it personal, enthusiastic, and reference the streak if it's notable. Keep it under 50 words."""
    
    try:
        generation_config = genai.types.GenerationConfig(
            max_output_tokens=100,
            temperature=0.8,
        )
        response = model.generate_content(prompt, generation_config=generation_config, request_options={'timeout': 10})
        return response.text.strip()
    except Exception as e:
        logger.error(f"AI motivation error: {e}")
        # Fallback motivational messages
        if streak >= 7:
            return f"Incredible! {streak} days strong! You're building a real habit here! 🔥"
        elif streak >= 3:
            return f"Amazing work! {streak} days in a row! Keep this momentum going! 💪"
        else:
            return "Great job! Every completion counts towards your success! 🎉"

async def ai_chat_assistant(user_message, user_habits_data):
    """AI assistant that answers questions about habits"""
    habits_summary = "\n".join([f"- {h[1]} (Streak: {calculate_streak(get_habit_completions(h[0]))} days)" 
                                 for h in user_habits_data]) if user_habits_data else "No habits yet"
    
    prompt = f"""You are a helpful habit-building coach. The user has these habits:
{habits_summary}

User question: "{user_message}"

Provide a helpful, concise response (2-3 sentences max). Be encouraging and actionable. If they ask about their habits, reference their actual data."""
    
    try:
        # Set timeout and generation config for faster response
        generation_config = genai.types.GenerationConfig(
            max_output_tokens=200,
            temperature=0.7,
        )
        response = model.generate_content(prompt, generation_config=generation_config, request_options={'timeout': 10})
        return response.text.strip()
    except Exception as e:
        logger.error(f"AI chat error: {e}")
        return "I'm having trouble getting a response right now. Try a simpler question or try again in a moment!"

# COMMAND HANDLERS
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    welcome_text = (
        "🎯 *Welcome to AI Habit Tracker Bot!*\n\n"
        "Build better habits with AI-powered insights!\n\n"
        "📋 *Available Commands:*\n\n"
        "🆕 *Getting Started:*\n"
        "/addhabit <name> - Add a new habit\n"
        "   Example: /addhabit Exercise\n\n"
        "📊 *Track Your Progress:*\n"
        "/myhabits - View all your habits\n"
        "/complete - Mark a habit as done today\n"
        "/stats - View detailed statistics\n\n"
        "🤖 *AI Features:*\n"
        "/ask <question> - Ask AI for advice\n"
        "   Example: /ask How stay consistent?\n\n"
        "⚙️ *Manage:*\n"
        "/deletehabit - Remove a habit\n"
        "/clr - Clear and reset chat\n"
        "/help - Show this message again\n\n"
        "💡 *Pro Tip:* Just chat naturally!\n"
        "• 'I want to start meditating'\n"
        "• 'Help me build a reading habit'\n"
        "• 'Why am I struggling with my habits?'\n\n"
        "Let's start building great habits! 💪"
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message"""
    await start(update, context)

async def add_habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new habit"""
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text(
            "📝 Please specify a habit name.\n"
            "Example: `/addhabit Drink water`",
            parse_mode='Markdown'
        )
        return
    
    habit_name = ' '.join(context.args).strip()
    
    if add_habit_to_db(user_id, habit_name):
        await update.message.reply_text(
            f"✅ Habit '{habit_name}' added successfully!\n\n"
            f"Use /complete to mark it as done today."
        )
    else:
        await update.message.reply_text(
            f"⚠️ You already have a habit called '{habit_name}'!"
        )

async def my_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all habits with streaks"""
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)
    
    if not habits:
        await update.message.reply_text(
            "You don't have any habits yet.\n"
            "Use /addhabit to create one, or just tell me what habit you want to build!"
        )
        return
    
    today = datetime.now().date().strftime('%Y-%m-%d')
    message = "📊 *Your Habits:*\n\n"
    
    for habit_id, habit_name, created_date in habits:
        dates = get_habit_completions(habit_id)
        streak = calculate_streak(dates)
        completed_today = today in dates
        status = "✅" if completed_today else "⭕"
        
        message += f"{status} *{habit_name}*\n"
        message += f"   🔥 Streak: {streak} days\n"
        message += f"   📅 Total: {len(dates)} days\n\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def complete_habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show buttons to mark habits as complete"""
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)
    
    if not habits:
        await update.message.reply_text(
            "You don't have any habits yet.\n"
            "Use /addhabit to create one!"
        )
        return
    
    today = datetime.now().date().strftime('%Y-%m-%d')
    keyboard = []
    
    for habit_id, habit_name, _ in habits:
        dates = get_habit_completions(habit_id)
        completed_today = today in dates
        emoji = "✅" if completed_today else "⭕"
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} {habit_name}",
                callback_data=f"complete:{habit_name}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Select a habit to mark as complete:",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if query.data.startswith("complete:"):
        habit_name = query.data.split(":", 1)[1]
        today = datetime.now().date().strftime('%Y-%m-%d')
        
        success, message = complete_habit_in_db(user_id, habit_name, today)
        
        if success:
            # Get updated streak
            habits = get_user_habits(user_id)
            for habit_id, h_name, _ in habits:
                if h_name == habit_name:
                    dates = get_habit_completions(habit_id)
                    streak = calculate_streak(dates)
                    
                    # Generate AI motivation
                    ai_message = await generate_motivation(habit_name, streak, len(dates))
                    
                    await query.edit_message_text(
                        f"🎉 '{habit_name}' completed!\n\n"
                        f"🔥 Streak: {streak} days | 📅 Total: {len(dates)}\n\n"
                        f"💬 {ai_message}"
                    )
                    break
        else:
            if message == "Already completed":
                await query.edit_message_text(
                    f"✅ You already completed '{habit_name}' today!\n"
                    f"🔥 Keep up the great work!"
                )
            else:
                await query.edit_message_text(f"❌ {message}")
    
    elif query.data.startswith("delete:"):
        habit_name = query.data.split(":", 1)[1]
        
        if delete_habit_from_db(user_id, habit_name):
            await query.edit_message_text(f"🗑️ Habit '{habit_name}' deleted.")
        else:
            await query.edit_message_text("❌ Habit not found!")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed statistics"""
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)
    
    if not habits:
        await update.message.reply_text(
            "You don't have any habits yet.\n"
            "Use /addhabit to create one!"
        )
        return
    
    habit_data = []
    total_completions = 0
    max_streak = 0
    
    for habit_id, habit_name, created_date in habits:
        dates = get_habit_completions(habit_id)
        streak = calculate_streak(dates)
        total_completions += len(dates)
        max_streak = max(max_streak, streak)
        habit_data.append((habit_name, streak, len(dates)))
    
    message = "📈 *Your Statistics:*\n\n"
    message += f"📊 Total Habits: {len(habits)}\n"
    message += f"✅ Total Completions: {total_completions}\n"
    message += f"🔥 Best Streak: {max_streak} days\n\n"
    message += "*Habit Details:*\n\n"
    
    # Sort by streak (descending)
    for habit_name, streak, total in sorted(habit_data, key=lambda x: x[1], reverse=True):
        message += f"• *{habit_name}*\n"
        message += f"  Streak: {streak} 🔥 | Total: {total} ✅\n\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def delete_habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show buttons to delete habits"""
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)
    
    if not habits:
        await update.message.reply_text(
            "You don't have any habits yet.\n"
            "Use /addhabit to create one!"
        )
        return
    
    keyboard = []
    for _, habit_name, _ in habits:
        keyboard.append([
            InlineKeyboardButton(
                f"🗑️ {habit_name}",
                callback_data=f"delete:{habit_name}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⚠️ Select a habit to delete:",
        reply_markup=reply_markup
    )

async def ask_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask AI for habit advice"""
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text(
            "💬 Ask me anything about building habits!\n\n"
            "Examples:\n"
            "• /ask How do I stay consistent?\n"
            "• /ask Why do I break streaks?\n"
            "• /ask Tips for morning routines"
        )
        return
    
    question = ' '.join(context.args)
    habits = get_user_habits(user_id)
    
    await update.message.reply_text("🤔 Thinking...")
    
    response = await ai_chat_assistant(question, habits)
    await update.message.reply_text(f"💡 {response}")

async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear the chat and show fresh start message"""
    clear_text = (
        "🧹 *Chat Cleared!*\n\n"
        "Starting fresh! Your habits are still saved.\n\n"
        "📋 *Quick Commands:*\n"
        "/myhabits - See your habits\n"
        "/complete - Mark habits done\n"
        "/stats - View your progress\n"
        "/help - Show all commands\n\n"
        "Ready to continue your journey! 💪"
    )
    await update.message.reply_text(clear_text, parse_mode='Markdown')

async def handle_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle natural language messages for habit creation and chat"""
    user_id = update.effective_user.id
    text = update.message.text.lower()
    
    # Keywords that suggest habit creation
    habit_keywords = ['want to', 'need to', 'should', 'help me', 'start', 'build', 'create habit']
    
    if any(keyword in text for keyword in habit_keywords):
        # Try to extract habit
        await update.message.reply_text("🤔 Let me understand that...")
        
        habit_name = await extract_habit_from_text(update.message.text)
        
        if habit_name:
            # Ask for confirmation
            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes, add it!", callback_data=f"add_habit:{habit_name}"),
                    InlineKeyboardButton("❌ No, cancel", callback_data="cancel_add")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"I think you want to add:\n\n*'{habit_name}'*\n\nIs this correct?",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "I couldn't quite understand that. Try:\n"
                "• 'I want to exercise daily'\n"
                "• 'Help me drink more water'\n\n"
                "Or use: /addhabit Habit Name"
            )
    else:
        # General question - use AI assistant
        habits = get_user_habits(user_id)
        await update.message.reply_text("💭 Let me think about that...")
        
        response = await ai_chat_assistant(update.message.text, habits)
        await update.message.reply_text(f"💡 {response}")

async def handle_habit_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle habit addition confirmation"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if query.data.startswith("add_habit:"):
        habit_name = query.data.split(":", 1)[1]
        
        if add_habit_to_db(user_id, habit_name):
            await query.edit_message_text(
                f"✅ Great! '{habit_name}' has been added to your habits!\n\n"
                f"Use /complete to mark it as done today."
            )
        else:
            await query.edit_message_text(
                f"⚠️ You already have a habit called '{habit_name}'!"
            )
    
    elif query.data == "cancel_add":
        await query.edit_message_text("❌ Cancelled. No habit was added.")

def main():
    """Start the bot"""
    # Initialize database
    init_db()
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addhabit", add_habit))
    application.add_handler(CommandHandler("myhabits", my_habits))
    application.add_handler(CommandHandler("complete", complete_habit))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("deletehabit", delete_habit))
    application.add_handler(CommandHandler("ask", ask_ai))
    application.add_handler(CommandHandler("clr", clear_chat))
    
    # Handle button callbacks
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(complete:|delete:)"))
    application.add_handler(CallbackQueryHandler(handle_habit_confirmation, pattern="^(add_habit:|cancel_add)"))
    
    # Handle natural language (must be last)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_natural_language))
    
    # Start the bot
    logger.info("🤖 AI-Powered Habit Tracker Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()