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
    # Mock BODYSTRUCTURE for multipart/alternative (text + HTML)
    bodystructure = (
        [
            (b"TEXT", b"PLAIN", ("CHARSET", "UTF-8"), None, None, "7BIT", 11, 1),
            (b"TEXT", b"HTML", ("CHARSET", "UTF-8"), None, None, "7BIT", 20, 1),
        ],
        b"ALTERNATIVE",
    )

    # Mock FETCH responses (two calls: BODYSTRUCTURE then BODY parts)
    mock_imap_client.fetch.side_effect = [
        {42: {b"BODYSTRUCTURE": bodystructure}},  # First call: BODYSTRUCTURE
        {42: {b"BODY[1]": b"Hello World", b"BODY[2]": b"<p>Hello World</p>"}},  # Second call: body parts
    ]

    # Fetch body
    text, html = await adapter.fetch_message_body("INBOX", 42)

    # Verify SELECT was called
    mock_imap_client.select_folder.assert_called_with("INBOX", readonly=True)

    # Verify FETCH was called twice (BODYSTRUCTURE then body parts)
    assert mock_imap_client.fetch.call_count == 2
    mock_imap_client.fetch.assert_any_call([42], ["BODYSTRUCTURE"])
    mock_imap_client.fetch.assert_any_call([42], ["BODY[1]", "BODY[2]"])

    # Verify body was decoded
    assert text == "Hello World"
    assert html == "<p>Hello World</p>"


@pytest.mark.asyncio
async def test_fetch_message_body_simple_text_plain(adapter, mock_imap_client):
    """Test fetching simple text/plain message body."""
    # Mock BODYSTRUCTURE for simple text/plain message
    bodystructure = (b"TEXT", b"PLAIN", ("CHARSET", "UTF-8"), None, None, "7BIT", 11, 1)

    # Mock FETCH responses
    mock_imap_client.fetch.side_effect = [
        {42: {b"BODYSTRUCTURE": bodystructure}},  # BODYSTRUCTURE
        {42: {b"BODY[TEXT]": b"Plain text only"}},  # Body content
    ]

    # Fetch body
    text, html = await adapter.fetch_message_body("INBOX", 42)

    # Verify correct part was fetched
    mock_imap_client.fetch.assert_any_call([42], ["BODY[TEXT]"])

    # Verify only text returned (no HTML)
    assert text == "Plain text only"
    assert html is None


@pytest.mark.asyncio
async def test_fetch_message_body_simple_text_html(adapter, mock_imap_client):
    """Test fetching simple text/html message body."""
    # Mock BODYSTRUCTURE for simple text/html message
    bodystructure = (b"TEXT", b"HTML", ("CHARSET", "UTF-8"), None, None, "7BIT", 20, 1)

    # Mock FETCH responses
    mock_imap_client.fetch.side_effect = [
        {42: {b"BODYSTRUCTURE": bodystructure}},  # BODYSTRUCTURE
        {42: {b"BODY[1]": b"<p>HTML only</p>"}},  # Body content
    ]

    # Fetch body
    text, html = await adapter.fetch_message_body("INBOX", 42)

    # Verify correct part was fetched
    mock_imap_client.fetch.assert_any_call([42], ["BODY[1]"])

    # Verify only HTML returned (no plain text)
    assert text is None
    assert html == "<p>HTML only</p>"


@pytest.mark.asyncio
async def test_fetch_message_body_no_text_parts(adapter, mock_imap_client):
    """Test fetching message with no text/html parts (attachments only)."""
    # Mock BODYSTRUCTURE for message with only attachments
    bodystructure = (
        [
            (b"APPLICATION", b"PDF", ("NAME", "doc.pdf"), None, None, "BASE64", 1024, None),
        ],
        b"MIXED",
    )

    # Mock FETCH response
    mock_imap_client.fetch.return_value = {42: {b"BODYSTRUCTURE": bodystructure}}

    # Fetch body
    text, html = await adapter.fetch_message_body("INBOX", 42)

    # Verify only BODYSTRUCTURE was fetched (no body parts)
    assert mock_imap_client.fetch.call_count == 1
    mock_imap_client.fetch.assert_called_once_with([42], ["BODYSTRUCTURE"])

    # Verify both are None
    assert text is None
    assert html is None


@pytest.mark.asyncio
async def test_fetch_message_body_message_not_found(adapter, mock_imap_client):
    """Test fetching body for non-existent message."""
    # Mock empty FETCH response
    mock_imap_client.fetch.return_value = {}

    # Fetch body
    text, html = await adapter.fetch_message_body("INBOX", 999)

    # Verify both are None
    assert text is None
    assert html is None


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
    """Test that delete_message permanently expunges."""
    # Delete message permanently
    await adapter.delete_message("INBOX", 42)

    # Verify folder selected (read-write)
    mock_imap_client.select_folder.assert_called_once_with("INBOX", readonly=False)

    # Verify flag and expunge
    mock_imap_client.add_flags.assert_called_once_with([42], ["\\Deleted"])
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


@pytest.mark.asyncio
async def test_parse_mime_encoded_subject_base64(adapter, mock_imap_client):
    """Test that Base64 MIME-encoded subjects are decoded."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [42]

    # Mock FETCH with Base64-encoded subject
    # =?UTF-8?B?SGVsbG8gV29ybGQ=?= → "Hello World"
    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=b"=?UTF-8?B?SGVsbG8gV29ybGQ=?=",
        from_=(Address(b"Test", None, b"test", b"example.com"),),
        sender=(Address(b"Test", None, b"test", b"example.com"),),
        reply_to=(Address(b"Test", None, b"test", b"example.com"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (b"\\Seen",),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Subject should be decoded
    assert len(result.messages) == 1
    assert result.messages[0].subject == "Hello World"


@pytest.mark.asyncio
async def test_parse_mime_encoded_subject_multipart(adapter, mock_imap_client):
    """Test that multi-part MIME-encoded subjects are decoded."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [1]

    # Real example from issue
    encoded = (
        b"=?UTF-8?B?W2NycmZwYS5jby56YV0gQ2xpZW50IGNvbmZpZ3VyYXRpb24gc2V0?= "
        b"=?UTF-8?B?dGluZ3MgZm9yIOKAnGRlbG1lQGNycmZwYS5jby56YeKAnS4=?="
    )

    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=encoded,
        from_=(Address(b"cPanel", None, b"cpanel", b"crrfpa.co.za"),),
        sender=(Address(b"cPanel", None, b"cpanel", b"crrfpa.co.za"),),
        reply_to=(Address(b"cPanel", None, b"cpanel", b"crrfpa.co.za"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        1: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Subject should be fully decoded
    # Note: The encoded subject contains Unicode quotes (U+201C and U+201D)
    expected = "[crrfpa.co.za] Client configuration settings for \u201cdelme@crrfpa.co.za\u201d."
    assert len(result.messages) == 1
    assert result.messages[0].subject == expected


@pytest.mark.asyncio
async def test_parse_mime_encoded_subject_quoted_printable(adapter, mock_imap_client):
    """Test that Quoted-printable MIME-encoded subjects are decoded."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [42]

    # =?UTF-8?Q?Jos=C3=A9?= → "José"
    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=b"=?UTF-8?Q?Jos=C3=A9?=",
        from_=(Address(b"Test", None, b"test", b"example.com"),),
        sender=(Address(b"Test", None, b"test", b"example.com"),),
        reply_to=(Address(b"Test", None, b"test", b"example.com"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Subject should be decoded
    assert len(result.messages) == 1
    assert result.messages[0].subject == "José"


@pytest.mark.asyncio
async def test_parse_plain_ascii_subject_unchanged(adapter, mock_imap_client):
    """Test that plain ASCII subjects are unchanged."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [42]

    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=b"Hello World",
        from_=(Address(b"Test", None, b"test", b"example.com"),),
        sender=(Address(b"Test", None, b"test", b"example.com"),),
        reply_to=(Address(b"Test", None, b"test", b"example.com"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Plain ASCII should work as before
    assert len(result.messages) == 1
    assert result.messages[0].subject == "Hello World"


@pytest.mark.asyncio
async def test_parse_mime_encoded_display_name(adapter, mock_imap_client):
    """Test that MIME-encoded display names are decoded."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [42]

    # Display name with non-ASCII: "José García"
    encoded_name = b"=?UTF-8?Q?Jos=C3=A9_Garc=C3=ADa?="

    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=b"Test",
        from_=(Address(encoded_name, None, b"jose", b"example.com"),),
        sender=(Address(encoded_name, None, b"jose", b"example.com"),),
        reply_to=(Address(encoded_name, None, b"jose", b"example.com"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Display name should be decoded
    assert len(result.messages) == 1
    assert result.messages[0].from_.name == "José García"


@pytest.mark.asyncio
async def test_parse_empty_subject(adapter, mock_imap_client):
    """Test that empty/None subject returns empty string."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [42]

    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=None,  # Empty subject
        from_=(Address(b"Test", None, b"test", b"example.com"),),
        sender=(Address(b"Test", None, b"test", b"example.com"),),
        reply_to=(Address(b"Test", None, b"test", b"example.com"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Empty subject should return empty string
    assert len(result.messages) == 1
    assert result.messages[0].subject == ""


@pytest.mark.asyncio
async def test_parse_mixed_encoded_and_plain_parts(adapter, mock_imap_client):
    """Test that mixed encoded and plain text parts are decoded correctly."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [42]

    # Mixed: plain text + encoded part
    # "Hello =?UTF-8?B?V29ybGQ=?=" → "Hello World"
    mixed_subject = b"Hello =?UTF-8?B?V29ybGQ=?="

    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=mixed_subject,
        from_=(Address(b"Test", None, b"test", b"example.com"),),
        sender=(Address(b"Test", None, b"test", b"example.com"),),
        reply_to=(Address(b"Test", None, b"test", b"example.com"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Mixed parts should be decoded correctly
    assert len(result.messages) == 1
    assert result.messages[0].subject == "Hello World"


@pytest.mark.asyncio
async def test_parse_malformed_mime_header_graceful_fallback(adapter, mock_imap_client):
    """Test that malformed MIME headers fall back gracefully."""
    # Mock SEARCH to return UID
    mock_imap_client.search.return_value = [42]

    # Malformed MIME header (missing closing ?=)
    malformed = b"=?UTF-8?B?SGVsbG8="

    mock_envelope = Envelope(
        date=datetime(2025, 12, 15, 10, 0, 0),
        subject=malformed,
        from_=(Address(b"Test", None, b"test", b"example.com"),),
        sender=(Address(b"Test", None, b"test", b"example.com"),),
        reply_to=(Address(b"Test", None, b"test", b"example.com"),),
        to=(Address(b"Test", None, b"test", b"example.com"),),
        cc=(),
        bcc=(),
        in_reply_to=b"",
        message_id=b"<123@example.com>",
    )

    mock_imap_client.fetch.return_value = {
        42: {
            b"ENVELOPE": mock_envelope,
            b"FLAGS": (),
            b"RFC822.SIZE": 1024,
            b"INTERNALDATE": datetime(2025, 12, 15, 10, 0, 0),
        }
    }

    # Query messages
    result = await adapter.query_messages("INBOX", Q.all(), limit=10)

    # Should not crash, should return raw string
    assert len(result.messages) == 1
    # Fallback should decode as UTF-8
    assert "UTF-8" in result.messages[0].subject or "SGVsbG8" in result.messages[0].subject


@pytest.mark.asyncio
async def test_select_folder_invalidates_cache_on_failure(adapter, mock_imap_client):
    """Test that folder cache is invalidated when SELECT fails."""
    # Step 1: Select valid folder (cache populated)
    mock_imap_client.select_folder.return_value = {
        b"EXISTS": 2,
        b"UNSEEN": 0,
        b"UIDNEXT": 3,
    }
    await adapter._select_folder("INBOX")
    assert adapter._selected_folder == "INBOX"
    assert adapter._selected_folder_readonly is True

    # Step 2: Select invalid folder (should invalidate cache)
    mock_imap_client.select_folder.side_effect = Exception(
        "select failed: Client tried to access nonexistent namespace"
    )

    from mailcore import FolderNotFoundError

    with pytest.raises(FolderNotFoundError) as exc_info:
        await adapter._select_folder("NONEXISTENT")

    # Verify domain exception raised with correct message
    assert exc_info.value.folder == "NONEXISTENT"
    assert "does not exist" in str(exc_info.value)

    # Cache should be invalidated
    assert adapter._selected_folder is None
    assert adapter._selected_folder_readonly is True


# append_message() tests


@pytest.mark.asyncio
async def test_append_message_builds_mime_and_returns_uid(adapter):
    """Test that append_message builds MIME message and returns UID from APPEND."""
    from mailcore import EmailAddress, MessageFlag

    # Simulate APPENDUID response: (uidvalidity, [uid])
    adapter._client.append.return_value = (12345, [42])

    uid = await adapter.append_message(
        folder="Drafts",
        from_=EmailAddress("me@example.com"),
        to=[EmailAddress("alice@example.com")],
        subject="Test Subject",
        body_text="Test body",
        flags={MessageFlag.DRAFT},
    )

    # Verify UID returned
    assert uid == 42

    # Verify append was called
    assert adapter._client.append.called
    call_args = adapter._client.append.call_args

    # Verify folder
    assert call_args.args[0] == "Drafts"

    # Verify MIME message (bytes)
    mime_bytes = call_args.args[1]
    assert isinstance(mime_bytes, bytes)
    assert b"From: me@example.com" in mime_bytes
    assert b"To: alice@example.com" in mime_bytes
    assert b"Subject: Test Subject" in mime_bytes
    assert b"Test body" in mime_bytes

    # Verify flags
    assert MessageFlag.DRAFT.value in call_args.kwargs["flags"]


@pytest.mark.asyncio
async def test_append_message_combines_flags_and_custom_flags(adapter):
    """Test that append_message combines standard and custom flags."""
    from mailcore import EmailAddress, MessageFlag

    adapter._client.append.return_value = (12345, [42])

    await adapter.append_message(
        folder="Drafts",
        from_=EmailAddress("me@example.com"),
        to=[EmailAddress("alice@example.com")],
        subject="Test",
        flags={MessageFlag.DRAFT, MessageFlag.SEEN},
        custom_flags={"$Forwarded", "$MDNSent"},
    )

    call_args = adapter._client.append.call_args
    flags = call_args.kwargs["flags"]

    # Verify both standard and custom flags present
    assert MessageFlag.DRAFT.value in flags
    assert MessageFlag.SEEN.value in flags
    assert "$Forwarded" in flags
    assert "$MDNSent" in flags


@pytest.mark.asyncio
async def test_append_message_excludes_bcc_from_mime(adapter):
    """Test that append_message does not include BCC in MIME headers (security)."""
    from mailcore import EmailAddress, MessageFlag

    adapter._client.append.return_value = (12345, [42])

    # Note: BCC is intentionally NOT a parameter in append_message (security requirement)
    await adapter.append_message(
        folder="Drafts",
        from_=EmailAddress("me@example.com"),
        to=[EmailAddress("alice@example.com")],
        subject="Test",
        body_text="Body",
        flags={MessageFlag.DRAFT},
    )

    call_args = adapter._client.append.call_args
    mime_bytes = call_args.args[1]

    # Verify BCC header not present
    assert b"Bcc:" not in mime_bytes


@pytest.mark.asyncio
async def test_append_message_handles_no_appenduid(adapter):
    """Test that append_message returns 0 if server doesn't provide APPENDUID."""
    from mailcore import EmailAddress, MessageFlag

    # No APPENDUID response
    adapter._client.append.return_value = None

    uid = await adapter.append_message(
        folder="Drafts",
        from_=EmailAddress("me@example.com"),
        to=[EmailAddress("alice@example.com")],
        subject="Test",
        flags={MessageFlag.DRAFT},
    )

    # Should return 0 when no APPENDUID
    assert uid == 0


# IDLE Protocol Tests (Story 3.28)


@pytest.mark.asyncio
async def test_select_folder_returns_dict_keys(adapter):
    """Test that select_folder() returns dict with expected keys."""
    # Mock IMAPClient response with byte keys
    adapter._client.select_folder.return_value = {
        b"EXISTS": 42,
        b"RECENT": 3,
        b"UIDVALIDITY": 1234567890,
    }

    result = await adapter.select_folder("INBOX")

    # Verify dict keys and values
    assert result == {"exists": 42, "recent": 3, "uidvalidity": 1234567890}
    adapter._client.select_folder.assert_called_once_with("INBOX")


@pytest.mark.asyncio
async def test_idle_start_raises_not_implemented(adapter):
    """Test that idle_start() raises NotImplementedError with helpful message."""
    import pytest

    with pytest.raises(NotImplementedError) as exc_info:
        await adapter.idle_start()

    # Verify error message guides user to mailcore-aioimaplib
    assert "mailcore-aioimaplib" in str(exc_info.value)
    assert "synchronous IMAPClient" in str(exc_info.value)


@pytest.mark.asyncio
async def test_idle_wait_raises_not_implemented(adapter):
    """Test that idle_wait() raises NotImplementedError."""
    import pytest

    with pytest.raises(NotImplementedError) as exc_info:
        await adapter.idle_wait(timeout=1800)

    assert "IDLE not supported" in str(exc_info.value)
    assert "mailcore-aioimaplib" in str(exc_info.value)


@pytest.mark.asyncio
async def test_idle_done_raises_not_implemented(adapter):
    """Test that idle_done() raises NotImplementedError."""
    import pytest

    with pytest.raises(NotImplementedError) as exc_info:
        await adapter.idle_done()

    assert "IDLE not supported" in str(exc_info.value)
    assert "mailcore-aioimaplib" in str(exc_info.value)


@pytest.mark.asyncio
async def test_uid_range_query_passes_sequence_set(adapter):
    """Test that UID range queries pass sequence set directly to IMAPClient.search().

    Bug fix: Story 3.28 - Q.uid_range() now returns ["4:*"] (sequence set),
    not ["UID", "4:*"] (invalid search criterion). Adapter passes it through unchanged.
    """
    from mailcore import Q

    # Mock IMAPClient.search to return UIDs
    adapter._client.search.return_value = [5, 6, 7]

    # Mock fetch to return empty dict (we only care about search criteria)
    adapter._client.fetch.return_value = {}

    # Mock folder status
    adapter._client.folder_status.return_value = {
        b"MESSAGES": 10,
        b"UNSEEN": 3,
        b"UIDNEXT": 8,
    }

    # Query with UID range
    query = Q.uid_range(4, "*")
    result = await adapter.query_messages("INBOX", query)

    # Verify search was called with sequence set (no "UID" prefix)
    adapter._client.search.assert_called_once()
    search_criteria = adapter._client.search.call_args[0][0]
    assert search_criteria == ["4:*"], f"Expected ['4:*'], got {search_criteria}"

    # Verify search returned UIDs (even though fetch is empty, we got the UIDs from search)
    assert result.total_matches == 3
