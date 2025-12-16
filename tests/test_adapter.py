"""Unit tests for IMAPClientAdapter.

Tests all 12 IMAPConnection methods with mocked IMAPClient responses.
Validates ThreadPoolExecutor pattern, folder caching, base64 decoding, and message parsing.
"""

from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from imapclient.response_types import Address, Envelope
from mailcore import MessageFlag, Q

from mailcore_imapclient import IMAPClientAdapter


@pytest.fixture
def mock_imap_client():
    """Create mocked IMAPClient instance."""
    client = Mock()
    # Mock login to return successfully
    client.login.return_value = None
    # Mock select_folder to return select info
    client.select_folder.return_value = {
        b"EXISTS": 100,
        b"UNSEEN": 5,
        b"UIDNEXT": 150,
    }
    return client


@pytest.fixture
def adapter(mock_imap_client):
    """Create IMAPClientAdapter with mocked IMAPClient."""
    with patch("mailcore_imapclient.adapter.IMAPClient", return_value=mock_imap_client):
        adapter = IMAPClientAdapter(
            host="imap.test.com",
            port=993,
            username="test@test.com",
            password="password",  # pragma: allowlist secret
            ssl=True,
        )
    return adapter


@pytest.mark.asyncio
async def test_init_connects_and_authenticates(mock_imap_client):
    """Test that __init__ connects and logs in immediately."""
    with patch("mailcore_imapclient.adapter.IMAPClient", return_value=mock_imap_client):
        adapter = IMAPClientAdapter(
            host="imap.test.com",
            port=993,
            username="test@test.com",
            password="password",  # pragma: allowlist secret
        )

    # Verify IMAPClient was created with correct params
    assert adapter._host == "imap.test.com"
    assert adapter._port == 993
    assert adapter._username == "test@test.com"

    # Verify login was called
    mock_imap_client.login.assert_called_once_with("test@test.com", "password")  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_run_sync_executes_in_executor(adapter, mock_imap_client):
    """Test that _run_sync delegates to ThreadPoolExecutor."""

    # Mock a sync function
    def sync_func(x, y):
        return x + y

    # Execute via _run_sync
    result = await adapter._run_sync(sync_func, 2, 3)

    assert result == 5


@pytest.mark.asyncio
async def test_select_folder_caching(adapter, mock_imap_client):
    """Test that folder selection is cached to avoid redundant SELECTs."""
    # First select
    await adapter._select_folder("INBOX")
    assert adapter._selected_folder == "INBOX"
    assert mock_imap_client.select_folder.call_count == 1

    # Second select to same folder - should be cached
    await adapter._select_folder("INBOX")
    assert mock_imap_client.select_folder.call_count == 1  # No additional call

    # Select different folder - should call again
    await adapter._select_folder("Sent")
    assert adapter._selected_folder == "Sent"
    assert mock_imap_client.select_folder.call_count == 2


@pytest.mark.asyncio
async def test_query_messages_converts_query_to_imap_criteria(adapter, mock_imap_client):
    """Test that query_messages converts Query to IMAP criteria and searches."""
    # Mock SEARCH to return UIDs
    mock_imap_client.search.return_value = [1, 2, 3]

    # Mock FETCH to return empty (we'll skip message parsing for this test)
    mock_imap_client.fetch.return_value = {}

    # Mock folder_status
    mock_imap_client.folder_status.return_value = {
        b"MESSAGES": 100,
        b"UNSEEN": 5,
        b"UIDNEXT": 150,
    }

    # Query unseen messages
    query = Q.unseen()
    await adapter.query_messages("INBOX", query, limit=10)

    # Verify SEARCH was called with correct criteria
    mock_imap_client.search.assert_called_once()
    search_criteria = mock_imap_client.search.call_args[0][0]
    assert "UNSEEN" in search_criteria or search_criteria == ["UNSEEN"]


@pytest.mark.asyncio
async def test_query_messages_fetches_metadata(adapter, mock_imap_client):
    """Test that query_messages fetches ENVELOPE, FLAGS, SIZE, DATE."""
    # Mock SEARCH
    mock_imap_client.search.return_value = [42]

    # Mock FETCH with envelope data
    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": Envelope(
                date=datetime(2025, 12, 15, 10, 0, 0),
                subject=b"Test Subject",
                from_=(Address(b"John Doe", None, b"john", b"example.com"),),
                sender=(Address(b"John Doe", None, b"john", b"example.com"),),
                reply_to=(Address(b"John Doe", None, b"john", b"example.com"),),
                to=(Address(b"Jane Doe", None, b"jane", b"example.com"),),
                cc=(),
                bcc=(),
                in_reply_to=b"",
                message_id=b"<msg-123@example.com>",
            ),
            b"FLAGS": (b"\\Seen",),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Mock folder_status
    mock_imap_client.folder_status.return_value = {
        b"MESSAGES": 100,
        b"UNSEEN": 5,
        b"UIDNEXT": 150,
    }

    # Query messages
    await adapter.query_messages("INBOX", Q.all())

    # Verify FETCH was called with correct fields
    mock_imap_client.fetch.assert_called_once()
    fetch_fields = mock_imap_client.fetch.call_args[0][1]
    assert "ENVELOPE" in fetch_fields
    assert "FLAGS" in fetch_fields
    assert "RFC822.SIZE" in fetch_fields
    assert "INTERNALDATE" in fetch_fields


@pytest.mark.asyncio
async def test_query_messages_paginates_with_limit_offset(adapter, mock_imap_client):
    """Test that query_messages handles pagination correctly."""
    # Mock SEARCH to return 100 UIDs
    all_uids = list(range(1, 101))
    mock_imap_client.search.return_value = all_uids

    # Mock FETCH to return empty (skip message parsing)
    mock_imap_client.fetch.return_value = {}

    # Mock folder_status
    mock_imap_client.folder_status.return_value = {
        b"MESSAGES": 100,
        b"UNSEEN": 5,
        b"UIDNEXT": 150,
    }

    # Query with limit=10, offset=20
    await adapter.query_messages("INBOX", Q.all(), limit=10, offset=20)

    # Verify FETCH was called with correct UID slice
    mock_imap_client.fetch.assert_called_once()
    fetched_uids = mock_imap_client.fetch.call_args[0][0]
    # UIDs are sorted newest first (reverse), then sliced
    expected_uids = sorted(all_uids, reverse=True)[20:30]
    assert fetched_uids == expected_uids


@pytest.mark.asyncio
async def test_fetch_message_body_selects_folder(adapter, mock_imap_client):
    """Test that fetch_message_body selects folder before fetching."""
    # Mock FETCH to return body
    mock_imap_client.fetch.return_value = {
        42: {
            b"BODY[TEXT]": b"Hello World",
            b"BODY[1.HTML]": b"<p>Hello World</p>",
        }
    }

    # Fetch body
    text, html = await adapter.fetch_message_body("INBOX", 42)

    # Verify SELECT was called
    mock_imap_client.select_folder.assert_called_with("INBOX", readonly=True)

    # Verify FETCH was called with correct fields
    mock_imap_client.fetch.assert_called_once_with([42], ["BODY[TEXT]", "BODY[1.HTML]"])

    # Verify body was decoded
    assert text == "Hello World"
    assert html == "<p>Hello World</p>"


@pytest.mark.asyncio
async def test_fetch_attachment_content_base64_decodes(adapter, mock_imap_client):
    """Test that fetch_attachment_content base64 decodes content (CRITICAL - Story 3.0)."""
    import base64

    # Original content
    original_content = b"This is a test attachment content"

    # Base64 encode it (simulating IMAPClient behavior)
    base64_content = base64.b64encode(original_content)

    # Mock FETCH to return base64-encoded content
    mock_imap_client.fetch.return_value = {
        42: {
            b"BODY[2]": base64_content,
        }
    }

    # Fetch attachment
    content = await adapter.fetch_attachment_content("INBOX", 42, "2")

    # Verify content was base64 decoded
    assert content == original_content


@pytest.mark.asyncio
async def test_update_message_flags_adds_seen_flag(adapter, mock_imap_client):
    """Test that update_message_flags adds \\Seen flag."""
    # Mock FETCH to return updated flags
    mock_imap_client.fetch.return_value = {
        42: {
            b"FLAGS": (b"\\Seen", b"\\Flagged"),
        }
    }

    # Update flags
    new_flags, custom_flags = await adapter.update_message_flags("INBOX", 42, add_flags={MessageFlag.SEEN})

    # Verify add_flags was called
    mock_imap_client.add_flags.assert_called_once_with([42], ["\\Seen"])

    # Verify updated flags returned
    assert MessageFlag.SEEN in new_flags
    assert MessageFlag.FLAGGED in new_flags


@pytest.mark.asyncio
async def test_move_message_uses_move_command(adapter, mock_imap_client):
    """Test that move_message uses MOVE command if available."""
    # Mock MOVE to return COPYUID response
    mock_imap_client.move.return_value = {42: 100}  # Old UID 42 → New UID 100

    # Move message
    new_uid = await adapter.move_message(42, "INBOX", "Archive")

    # Verify MOVE was called
    mock_imap_client.move.assert_called_once_with([42], "Archive")

    # Verify new UID returned
    assert new_uid == 100


@pytest.mark.asyncio
async def test_copy_message_uses_copy_command(adapter, mock_imap_client):
    """Test that copy_message uses COPY command."""
    # Mock COPY to return COPYUID response
    mock_imap_client.copy.return_value = {42: 100}  # Old UID 42 → New UID 100

    # Copy message
    new_uid = await adapter.copy_message(42, "INBOX", "Archive")

    # Verify COPY was called
    mock_imap_client.copy.assert_called_once_with([42], "Archive")

    # Verify new UID returned
    assert new_uid == 100


@pytest.mark.asyncio
async def test_delete_message_permanent_true_expunges(adapter, mock_imap_client):
    """Test that delete_message with permanent=True expunges immediately."""
    # Delete message permanently
    await adapter.delete_message("INBOX", 42, permanent=True)

    # Verify STORE \\Deleted was called
    mock_imap_client.add_flags.assert_called_once_with([42], ["\\Deleted"])

    # Verify EXPUNGE was called
    mock_imap_client.expunge.assert_called_once()


@pytest.mark.asyncio
async def test_get_folders_calls_list(adapter, mock_imap_client):
    """Test that get_folders calls LIST and returns FolderInfo list."""
    # Mock LIST to return folder list
    mock_imap_client.list_folders.return_value = [
        ((b"\\HasNoChildren",), b"/", b"INBOX"),
        ((b"\\HasChildren", b"\\Sent"), b"/", b"Sent"),
    ]

    # Get folders
    folders = await adapter.get_folders()

    # Verify LIST was called
    mock_imap_client.list_folders.assert_called_once()

    # Verify folders returned
    assert len(folders) == 2
    assert folders[0].name == "INBOX"
    assert folders[0].has_children is False
    assert folders[1].name == "Sent"
    assert folders[1].has_children is True


@pytest.mark.asyncio
async def test_get_folder_status_calls_status(adapter, mock_imap_client):
    """Test that get_folder_status calls STATUS and returns FolderStatus."""
    # Mock STATUS to return folder status
    mock_imap_client.folder_status.return_value = {
        b"MESSAGES": 100,
        b"UNSEEN": 5,
        b"UIDNEXT": 150,
    }

    # Get folder status
    status = await adapter.get_folder_status("INBOX")

    # Verify STATUS was called
    mock_imap_client.folder_status.assert_called_once_with("INBOX", ["MESSAGES", "UNSEEN", "UIDNEXT"])

    # Verify status returned
    assert status.message_count == 100
    assert status.unseen_count == 5
    assert status.uidnext == 150


@pytest.mark.asyncio
async def test_create_folder_calls_create(adapter, mock_imap_client):
    """Test that create_folder calls CREATE."""
    # Mock LIST to return created folder
    mock_imap_client.list_folders.return_value = [
        ((b"\\HasNoChildren",), b"/", b"TestFolder"),
    ]

    # Create folder
    folder_info = await adapter.create_folder("TestFolder")

    # Verify CREATE was called
    mock_imap_client.create_folder.assert_called_once_with("TestFolder")

    # Verify folder info returned
    assert folder_info.name == "TestFolder"


@pytest.mark.asyncio
async def test_delete_folder_calls_delete(adapter, mock_imap_client):
    """Test that delete_folder calls DELETE."""
    # Delete folder
    await adapter.delete_folder("TestFolder")

    # Verify DELETE was called
    mock_imap_client.delete_folder.assert_called_once_with("TestFolder")


@pytest.mark.asyncio
async def test_rename_folder_calls_rename(adapter, mock_imap_client):
    """Test that rename_folder calls RENAME."""
    # Mock LIST to return renamed folder
    mock_imap_client.list_folders.return_value = [
        ((b"\\HasNoChildren",), b"/", b"NewName"),
    ]

    # Rename folder
    folder_info = await adapter.rename_folder("OldName", "NewName")

    # Verify RENAME was called
    mock_imap_client.rename_folder.assert_called_once_with("OldName", "NewName")

    # Verify folder info returned
    assert folder_info.name == "NewName"


@pytest.mark.asyncio
async def test_execute_raw_command_passthrough(adapter, mock_imap_client):
    """Test that execute_raw_command passes through to IMAPClient."""
    # Mock a raw command
    mock_imap_client.search.return_value = [1, 2, 3]

    # Execute raw command
    result = await adapter.execute_raw_command("search", ["UNSEEN"])

    # Verify command was called
    mock_imap_client.search.assert_called_once_with(["UNSEEN"])

    # Verify result returned
    assert result == [1, 2, 3]


@pytest.mark.asyncio
async def test_parse_envelope_creates_email_addresses(adapter):
    """Test that _parse_envelope_address creates EmailAddress objects."""
    # Mock ENVELOPE address
    addr = Address(b"John Doe", None, b"john", b"example.com")

    # Parse address
    email_addr = adapter._parse_envelope_address(addr)

    # Verify EmailAddress created correctly
    assert email_addr.email == "john@example.com"
    assert email_addr.name == "John Doe"


@pytest.mark.asyncio
async def test_parse_flags_separates_standard_and_custom(adapter):
    """Test that _parse_flags separates standard flags from custom flags."""
    # Mock FLAGS with standard and custom flags
    flags = (b"\\Seen", b"\\Flagged", b"CustomFlag")

    # Parse flags
    standard_flags, custom_flags = adapter._parse_flags(flags)

    # Verify standard flags converted to MessageFlag enum
    assert MessageFlag.SEEN in standard_flags
    assert MessageFlag.FLAGGED in standard_flags

    # Verify custom flags kept as strings
    assert "CustomFlag" in custom_flags


@pytest.mark.asyncio
async def test_flag_to_imap_conversion(adapter):
    """Test that _flag_to_imap converts MessageFlag to IMAP string."""
    # Convert flags
    assert adapter._flag_to_imap(MessageFlag.SEEN) == "\\Seen"
    assert adapter._flag_to_imap(MessageFlag.FLAGGED) == "\\Flagged"
    assert adapter._flag_to_imap(MessageFlag.ANSWERED) == "\\Answered"


@pytest.mark.asyncio
async def test_query_messages_returns_messagelist_with_metadata(adapter, mock_imap_client):
    """Test that query_messages returns MessageList with pagination metadata."""
    # Mock SEARCH to return 100 UIDs
    mock_imap_client.search.return_value = list(range(1, 101))

    # Mock FETCH to return empty (skip message parsing)
    mock_imap_client.fetch.return_value = {}

    # Mock folder_status
    mock_imap_client.folder_status.return_value = {
        b"MESSAGES": 100,
        b"UNSEEN": 5,
        b"UIDNEXT": 150,
    }

    # Query with limit
    result = await adapter.query_messages("INBOX", Q.all(), limit=50)

    # Verify MessageList metadata
    assert result.total_matches == 100
    assert result.total_in_folder == 100
    assert result.folder == "INBOX"


@pytest.mark.asyncio
async def test_query_messages_with_limit_zero_returns_count_only(adapter, mock_imap_client):
    """Test that limit=0 returns correct total_matches without fetching messages.

    This is used by Folder.count() to get message count without expensive FETCH.
    Regression test for bug where limit=0 incorrectly returned total_matches=0.
    """
    # Mock SEARCH to return 5 UIDs (e.g., 5 unseen messages)
    mock_imap_client.search.return_value = [101, 102, 103, 104, 105]

    # Mock folder_status (for total_in_folder)
    mock_imap_client.folder_status.return_value = {
        b"MESSAGES": 7,
        b"UNSEEN": 5,
        b"UIDNEXT": 106,
    }

    # Query with limit=0 (count only, don't fetch)
    result = await adapter.query_messages("INBOX", Q.unseen(), limit=0)

    # Verify no messages returned (limit=0)
    assert len(result.messages) == 0

    # Verify correct count returned (NOT 0 - this was the bug!)
    assert result.total_matches == 5

    # Verify folder stats correct
    assert result.total_in_folder == 7
    assert result.folder == "INBOX"

    # Verify optimization: FETCH was never called (no messages to fetch)
    mock_imap_client.fetch.assert_not_called()
