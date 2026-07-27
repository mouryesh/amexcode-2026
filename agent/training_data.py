"""agent/training_data.py — labelled utterances used to TRAIN the intent classifier.

Deliberately disjoint from tests/test_classifier.py's held-out set: training
data teaches the model, the test set measures whether it generalises to
phrasing it has never seen. Reusing the same sentences in both would make the
reported accuracy meaningless.

Includes a "clarify" bucket of vague/off-topic utterances as its own trained
class — softmax always distributes probability across all learned classes, so
without an explicit "none of the above" class, a stray greeting could get
confidently (and wrongly) assigned to a real category instead of falling
through to a clarifying question.
"""
from __future__ import annotations

TRAINING_EXAMPLES: list[tuple[str, str]] = [
    # fee_waiver
    ("Can you get rid of this late fee?", "fee_waiver"),
    ("I got charged a late fee, can it be removed?", "fee_waiver"),
    ("Please remove the late payment charge", "fee_waiver"),
    ("I'd like this fee taken off my account", "fee_waiver"),
    ("Is there any way to waive this late charge?", "fee_waiver"),
    ("This late fee seems unfair, can it be waived?", "fee_waiver"),
    ("I was charged extra for paying late, please reverse it", "fee_waiver"),
    ("Can I get my late fee refunded?", "fee_waiver"),
    ("I need this fee credited back to me", "fee_waiver"),
    ("Would you mind waiving my late payment fee?", "fee_waiver"),
    ("There's a late fee on my statement I'd like removed", "fee_waiver"),
    ("Can you cancel the late fee that was applied?", "fee_waiver"),
    ("I missed my due date and got charged, can you help waive it?", "fee_waiver"),
    ("Please reverse the penalty fee on my account", "fee_waiver"),
    ("I want a waiver for the late charge this month", "fee_waiver"),

    # credit_limit_increase
    ("Can I get a higher credit limit?", "credit_limit_increase"),
    ("I'd like to request a limit increase", "credit_limit_increase"),
    ("My limit feels too low, can it be raised?", "credit_limit_increase"),
    ("Is it possible to bump up my credit line?", "credit_limit_increase"),
    ("I need more available credit on my card", "credit_limit_increase"),
    ("Can you increase my spending limit?", "credit_limit_increase"),
    ("I'd like a review for a higher credit limit", "credit_limit_increase"),
    ("Please consider raising my card limit", "credit_limit_increase"),
    ("I want to apply for a credit line increase", "credit_limit_increase"),
    ("Can my available credit be extended?", "credit_limit_increase"),
    ("I'm hoping to get a bigger limit on this card", "credit_limit_increase"),
    ("Could you look into increasing my credit ceiling?", "credit_limit_increase"),
    ("I need a higher limit for an upcoming purchase", "credit_limit_increase"),
    ("Please raise my current credit limit", "credit_limit_increase"),
    ("Can you approve a limit bump for my account?", "credit_limit_increase"),

    # card_replacement
    ("My card is cracked, can I get a new one?", "card_replacement"),
    ("The chip on my card stopped working, need a replacement", "card_replacement"),
    ("Can you send me a new physical card?", "card_replacement"),
    ("My card is worn out, I need a reissue", "card_replacement"),
    ("The magnetic strip isn't reading anymore, please replace it", "card_replacement"),
    ("I need a fresh card, mine is damaged", "card_replacement"),
    ("Can I request a new card to be mailed to me?", "card_replacement"),
    ("My current card is broken, please send a replacement", "card_replacement"),
    ("I'd like to reissue my card, it's not scanning properly", "card_replacement"),
    ("Can you get me a new card, this one is falling apart", "card_replacement"),
    ("My card got bent and won't work in machines anymore", "card_replacement"),
    ("Please send a replacement card, mine is unusable", "card_replacement"),
    ("I need a new card issued, the current one is defective", "card_replacement"),

    # card_block
    ("I think my card was stolen, please block it", "card_block"),
    ("I can't find my card anywhere, freeze it please", "card_block"),
    ("Someone took my wallet with my card in it", "card_block"),
    ("Please deactivate my card immediately, it's missing", "card_block"),
    ("I misplaced my card, can you put a hold on it?", "card_block"),
    ("My card got stolen at the store", "card_block"),
    ("Please block my card right now, I lost it", "card_block"),
    ("I dropped my wallet and can't find my card", "card_block"),
    ("Can you suspend my card, I think it's been taken", "card_block"),
    ("I need my card frozen urgently, it's gone missing", "card_block"),
    ("Please lock my card, someone might have stolen it", "card_block"),
    ("My card isn't in my bag anymore, please block it", "card_block"),

    # explain_charge
    ("There's a charge on my statement I don't recognize", "explain_charge"),
    ("Can you tell me what this transaction is for?", "explain_charge"),
    ("I see an unfamiliar amount on my bill", "explain_charge"),
    ("What does this charge on my account mean?", "explain_charge"),
    ("I don't understand this line item on my statement", "explain_charge"),
    ("Can you break down this transaction for me?", "explain_charge"),
    ("There's a charge I can't place, can you clarify it?", "explain_charge"),
    ("Please explain what this recent charge was for", "explain_charge"),
    ("I'm confused about a transaction on my card", "explain_charge"),
    ("Can you tell me more details about this purchase?", "explain_charge"),
    ("What is this merchant charge I'm seeing?", "explain_charge"),
    ("I need clarity on a recent transaction amount", "explain_charge"),

    # account_info
    ("Can you tell me how much I owe right now?", "account_info"),
    ("What's my available credit at the moment?", "account_info"),
    ("I want to check my current account balance", "account_info"),
    ("Can you give me my outstanding balance?", "account_info"),
    ("What's the total on my card right now?", "account_info"),
    ("I'd like a summary of my account status", "account_info"),
    ("Can you pull up my current limit and balance?", "account_info"),
    ("How much credit do I have left to use?", "account_info"),
    ("What's my due amount for this cycle?", "account_info"),
    ("Can you tell me my account details?", "account_info"),
    ("I want to know my current outstanding amount", "account_info"),
    ("Please share my balance and limit info", "account_info"),

    # reset_pin
    ("I can't remember my PIN, can you reset it?", "reset_pin"),
    ("I need a new PIN for my card", "reset_pin"),
    ("My PIN isn't working at the ATM", "reset_pin"),
    ("Can you help me set up a new PIN?", "reset_pin"),
    ("I forgot the PIN for my card, please reset it", "reset_pin"),
    ("I'd like to change my current PIN number", "reset_pin"),
    ("My card PIN got blocked, can you reset it?", "reset_pin"),
    ("Can I get a PIN reset for my account?", "reset_pin"),
    ("I keep entering the wrong PIN, please reset it", "reset_pin"),
    ("Please issue me a new PIN, I've forgotten mine", "reset_pin"),
    ("I need to reset my card's PIN code", "reset_pin"),

    # update_address
    ("I recently moved, can you update my mailing address?", "update_address"),
    ("Please change the address on file for my account", "update_address"),
    ("I have a new home address to update", "update_address"),
    ("Can you update where my statements get sent?", "update_address"),
    ("My address changed, please update your records", "update_address"),
    ("I need to update my shipping address for the card", "update_address"),
    ("Can you change my registered address?", "update_address"),
    ("I moved to a new place, update my contact address please", "update_address"),
    ("Please update my current residential address", "update_address"),
    ("I want to change the address linked to my account", "update_address"),
    ("Can you correct my mailing address on file?", "update_address"),

    # hardship
    ("I recently lost my job and I'm struggling to pay", "hardship"),
    ("I'm going through a tough financial time right now", "hardship"),
    ("I can't make my payments this month, I'm unemployed", "hardship"),
    ("Money has been really tight since I got laid off", "hardship"),
    ("I'm behind on payments because of a medical emergency", "hardship"),
    ("I don't have the funds to pay my bill this month", "hardship"),
    ("I've fallen on hard times and can't keep up with payments", "hardship"),
    ("I lost my income source and I'm worried about my bills", "hardship"),
    ("I'm having serious financial difficulty right now", "hardship"),
    ("I can't afford to pay anything on my card this month", "hardship"),
    ("I'm struggling since I was let go from my job", "hardship"),
    ("Things have been hard financially, I can't pay right now", "hardship"),
    # Mixed-signal: a specific request riding along with distress language.
    # Hardship must win even when another intent's keywords are also present.
    ("I can't pay and I want my late fee waived", "hardship"),
    ("I lost my job, can you also raise my credit limit", "hardship"),
    ("I can't afford this, please just block my card", "hardship"),
    ("I'm struggling financially, can you explain this charge", "hardship"),
    ("Since I was laid off I can't pay, please waive the fee", "hardship"),
    ("I can't afford my bill this month and need a new PIN too", "hardship"),

    # dispute_transaction
    ("I never made this purchase, I want to dispute it", "dispute_transaction"),
    ("There's a fraudulent charge on my card", "dispute_transaction"),
    ("Someone used my card without my permission", "dispute_transaction"),
    ("I want to report an unauthorized transaction", "dispute_transaction"),
    ("This purchase wasn't made by me, please dispute it", "dispute_transaction"),
    ("I need to challenge a charge I didn't authorize", "dispute_transaction"),
    ("There's a transaction I don't recall making at all", "dispute_transaction"),
    ("I think my card details were used fraudulently", "dispute_transaction"),
    ("Please file a dispute for this suspicious charge", "dispute_transaction"),
    ("I want to contest a payment I never approved", "dispute_transaction"),
    ("This charge is not mine, I'd like to dispute it", "dispute_transaction"),

    # payment_issue
    ("My payment didn't go through this month", "payment_issue"),
    ("I tried to pay my bill but it failed", "payment_issue"),
    ("The autopay didn't process correctly", "payment_issue"),
    ("My online payment got declined", "payment_issue"),
    ("I'm having trouble making a payment on the app", "payment_issue"),
    ("My scheduled payment seems to have not gone through", "payment_issue"),
    ("There was an error processing my payment", "payment_issue"),
    ("I keep getting a payment failure error", "payment_issue"),
    ("My bank transfer for the bill didn't complete", "payment_issue"),
    ("The payment portal isn't accepting my payment", "payment_issue"),
    ("I can't get my payment to go through today", "payment_issue"),

    # profile_update
    ("I want to update my email on file", "profile_update"),
    ("Can you change the phone number linked to my account?", "profile_update"),
    ("I need to update my contact information", "profile_update"),
    ("Please change my email address for notifications", "profile_update"),
    ("I got a new phone number, please update it", "profile_update"),
    ("Can you update my profile with a new email?", "profile_update"),
    ("I'd like to change my registered mobile number", "profile_update"),
    ("Please update the contact details on my account", "profile_update"),
    ("I want to switch the email associated with my card", "profile_update"),
    ("Can you correct my phone number in your system?", "profile_update"),
    ("I need my email updated to a new address", "profile_update"),

    # clarify — deliberately vague / off-topic / no identifiable request.
    # An explicit "none of the above" class, not just low confidence on a real
    # one — softmax always sums to 1 across trained classes, so without this
    # bucket a stray greeting has nowhere honest to land.
    ("hello", "clarify"),
    ("hi there", "clarify"),
    ("hey, are you there?", "clarify"),
    ("thanks", "clarify"),
    ("thank you so much", "clarify"),
    ("I have a question", "clarify"),
    ("can you help me with something", "clarify"),
    ("I need some assistance", "clarify"),
    ("is anyone there", "clarify"),
    ("good morning", "clarify"),
    ("okay", "clarify"),
    ("can we talk", "clarify"),
]
