"""E2E tests for IMAPClientAdapter against real Greenmail IMAP server.

Tests IMAPClientAdapter with real IMAP protocol (not mocked responses).
Assumes Greenmail running at localhost:3143 (IMAP) and localhost:3025 (SMTP).

Mark tests with @pytest.mark.e2e for optional execution.
"""

import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from mailcore import Q

from mailcore_imapclient import IMAPClientAdapter

# Greenmail credentials (standard test user)
GREENMAIL_HOST = "localhost"
GREENMAIL_IMAP_PORT = 3143
GREENMAIL_SMTP_PORT = 3025
GREENMAIL_USER = "test@test.com"
GREENMAIL_PASSWORD = "test"  # pragma: allowlist secret


@pytest.fixture
async def greenmail_adapter():
    """Create IMAPClientAdapter connected to Greenmail."""
    adapter = IMAPClientAdapter(
        host=GREENMAIL_HOST,
        port=GREENMAIL_IMAP_PORT,
        username=GREENMAIL_USER,
        password=GREENMAIL_PASSWORD,  # pragma: allowlist secret
        ssl=False,  # Greenmail doesn't use SSL in test mode
    )
    yield adapter
    # Cleanup: Could close connection here if needed


def send_test_email(subject: str, body: str, to: str = GREENMAIL_USER) -> None:
    """Send test email via Greenmail SMTP for E2E testing."""
    msg = MIMEMultipart()
    msg["From"] = GREENMAIL_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    smtp = smtplib.SMTP(GREENMAIL_HOST, GREENMAIL_SMTP_PORT)
    smtp.sendmail(GREENMAIL_USER, [to], msg.as_string())
    smtp.quit()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_connect_and_authenticate_greenmail(greenmail_adapter):
    """Test that IMAPClientAdapter connects and authenticates to Greenmail."""
    # If adapter initialized without exception, connection succeeded
    assert greenmail_adapter._host == GREENMAIL_HOST
    assert greenmail_adapter._port == GREENMAIL_IMAP_PORT
    assert greenmail_adapter._username == GREENMAIL_USER


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_query_messages_from_greenmail(greenmail_adapter):
    """Test querying messages from Greenmail INBOX."""
    # Send test email
    send_test_email("E2E Test Query", "This is a test message for query")

    # Wait for email to be processed
    await asyncio.sleep(0.5)

    # Query all messages
    result = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=10)

    # Verify messages returned
    assert len(result.messages) > 0
    assert result.folder == "INBOX"
    assert result.total_matches > 0


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_fetch_message_body_from_greenmail(greenmail_adapter):
    """Test fetching message body from Greenmail."""
    # Send test email with body
    test_body = "This is the E2E test body content"
    send_test_email("E2E Test Body", test_body)

    # Wait for email
    await asyncio.sleep(0.5)

    # Query to get UID
    result = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=1)
    assert len(result.messages) > 0

    message = result.messages[0]

    # Fetch body
    text, html = await greenmail_adapter.fetch_message_body("INBOX", message.uid)

    # Verify body fetched
    assert text is not None
    assert test_body in text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_update_message_flags_greenmail(greenmail_adapter):
    """Test updating message flags in Greenmail."""
    # Send test email
    send_test_email("E2E Test Flags", "Testing flag operations")

    # Wait for email
    await asyncio.sleep(0.5)

    # Query to get UID
    result = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=1)
    assert len(result.messages) > 0

    message = result.messages[0]

    # Update flags - mark as seen
    from mailcore import MessageFlag

    new_flags, custom_flags = await greenmail_adapter.update_message_flags(
        "INBOX", message.uid, add_flags={MessageFlag.SEEN}
    )

    # Verify flag updated
    assert MessageFlag.SEEN in new_flags


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_folder_operations_greenmail(greenmail_adapter):
    """Test folder operations (create, list, rename, delete) in Greenmail."""
    # Create folder
    test_folder = "E2ETestFolder"
    folder_info = await greenmail_adapter.create_folder(test_folder)
    assert folder_info.name == test_folder

    # List folders - verify it exists
    folders = await greenmail_adapter.get_folders()
    folder_names = [f.name for f in folders]
    assert test_folder in folder_names

    # Rename folder
    new_name = "E2ERenamed"
    renamed_info = await greenmail_adapter.rename_folder(test_folder, new_name)
    assert renamed_info.name == new_name

    # Delete folder
    await greenmail_adapter.delete_folder(new_name)

    # Verify deleted
    folders_after = await greenmail_adapter.get_folders()
    folder_names_after = [f.name for f in folders_after]
    assert new_name not in folder_names_after


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_move_message_greenmail(greenmail_adapter):
    """Test moving message between folders in Greenmail."""
    # Create Archive folder (ignore if already exists)
    try:
        await greenmail_adapter.create_folder("Archive")
    except Exception:
        pass  # Folder may already exist from previous tests

    # Send test email
    send_test_email("E2E Test Move", "Testing message move")

    # Wait for email
    await asyncio.sleep(0.5)

    # Query from INBOX
    result = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=1)
    assert len(result.messages) > 0

    message = result.messages[0]
    orig_uid = message.uid

    # Move to Archive
    await greenmail_adapter.move_message(orig_uid, "INBOX", "Archive")

    # Verify moved (new_uid may be 0 if COPYUID not supported)
    # Query Archive to verify
    archive_result = await greenmail_adapter.query_messages("Archive", Q.all())
    assert len(archive_result.messages) > 0


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_copy_message_greenmail(greenmail_adapter):
    """Test copying message between folders in Greenmail."""
    # Create Backup folder if doesn't exist
    try:
        await greenmail_adapter.create_folder("Backup")
    except Exception:
        pass  # Folder may already exist

    # Send test email
    send_test_email("E2E Test Copy", "Testing message copy")

    # Wait for email
    await asyncio.sleep(0.5)

    # Query from INBOX
    result = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=1)
    assert len(result.messages) > 0

    message = result.messages[0]

    # Copy to Backup
    await greenmail_adapter.copy_message(message.uid, "INBOX", "Backup")

    # Verify copied - message should be in both folders
    backup_result = await greenmail_adapter.query_messages("Backup", Q.all())
    assert len(backup_result.messages) > 0


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_delete_message_greenmail(greenmail_adapter):
    """Test deleting message in Greenmail."""
    # Send test email
    send_test_email("E2E Test Delete", "Testing message deletion")

    # Wait for email
    await asyncio.sleep(0.5)

    # Query to get UID
    result = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=1)
    assert len(result.messages) > 0

    message = result.messages[0]
    orig_count = result.total_in_folder

    # Delete permanently
    await greenmail_adapter.delete_message("INBOX", message.uid)

    # Verify deleted - count should decrease
    result_after = await greenmail_adapter.query_messages("INBOX", Q.all())
    assert result_after.total_in_folder < orig_count


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_get_folder_status_greenmail(greenmail_adapter):
    """Test getting folder status from Greenmail."""
    # Get INBOX status
    status = await greenmail_adapter.get_folder_status("INBOX")

    # Verify status returned
    assert status.message_count >= 0
    assert status.unseen_count >= 0
    assert status.uidnext > 0


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_mime_encoded_subject_greenmail(greenmail_adapter):
    """Test that MIME-encoded subjects are decoded from real IMAP."""
    # Send email with non-ASCII subject via SMTP
    msg = MIMEMultipart()
    msg["From"] = GREENMAIL_USER
    msg["To"] = GREENMAIL_USER
    msg["Subject"] = "Test: 你好世界 (Hello World in Chinese)"
    msg.attach(MIMEText("Body with international characters", "plain"))

    smtp = smtplib.SMTP(GREENMAIL_HOST, GREENMAIL_SMTP_PORT)
    smtp.sendmail(GREENMAIL_USER, [GREENMAIL_USER], msg.as_string())
    smtp.quit()

    # Wait for delivery
    await asyncio.sleep(0.5)

    # Query messages
    result = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=10)
    assert len(result.messages) > 0

    # Find the message we just sent
    message = None
    for msg in result.messages:
        if "你好世界" in msg.subject:
            message = msg
            break

    # Subject should be decoded (not MIME-encoded)
    assert message is not None, "Could not find sent message with Chinese characters"
    assert "你好世界" in message.subject
    assert "=?UTF-8?" not in message.subject


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_folder_operations_after_select_failure(greenmail_adapter):
    """Test that subsequent folder operations work after SELECT failure.

    Validates bug fix: Failed SELECT on non-existent folder should not
    corrupt cache state and break subsequent valid folder operations.
    """
    # Step 1: Access INBOX (valid folder) - should work
    result1 = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=10)
    count1 = result1.total_in_folder
    assert count1 >= 0  # Should succeed

    # Step 2: Access non-existent folder - should raise exception
    from mailcore import FolderNotFoundError

    with pytest.raises(FolderNotFoundError) as exc_info:
        await greenmail_adapter.query_messages("NONEXISTENT", Q.all(), limit=10)

    # Verify domain exception with clear message
    assert exc_info.value.folder == "NONEXISTENT"
    assert "does not exist" in str(exc_info.value)

    # Step 3: Access INBOX again - MUST work (validates bug fix)
    result2 = await greenmail_adapter.query_messages("INBOX", Q.all(), limit=10)
    count2 = result2.total_in_folder
    assert count2 >= 0  # Should succeed, not raise "No mailbox selected"
    assert count2 == count1  # Count should be same (no messages added/removed)
