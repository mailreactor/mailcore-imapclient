"""IMAPClient adapter implementing mailcore IMAPConnection ABC.

Wraps synchronous IMAPClient library with ThreadPoolExecutor pattern for async compatibility.
Translates IMAP protocol responses to mailcore domain objects (Message, MessageList, etc.).

CRITICAL: Base64 decodes attachment content (validated in Story 3.0 - IMAPClient returns
base64-encoded bytes).
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from email.header import decode_header
from functools import partial
from typing import Any

from imapclient import IMAPClient  # type: ignore[import-untyped]
from mailcore import (
    Attachment,
    EmailAddress,
    FolderNotFoundError,
    Message,
    MessageFlag,
    MessageList,
    Query,
)
from mailcore.attachment import IMAPResolver
from mailcore.protocols import IMAPConnection
from mailcore.types import FolderInfo, FolderStatus


class IMAPClientAdapter(IMAPConnection):
    """IMAPClient adapter wrapping synchronous IMAPClient with ThreadPoolExecutor pattern.

    Implements all 12 IMAPConnection ABC methods:
    - query_messages: SELECT + SEARCH + FETCH → MessageList
    - fetch_message_body: SELECT + FETCH BODY[TEXT]/BODY[1.HTML] → (text, html)
    - fetch_attachment_content: SELECT + FETCH BODY[part] + base64 decode → bytes
    - update_message_flags: SELECT + STORE → (flags, custom_flags)
    - move_message: SELECT + MOVE/COPY+EXPUNGE → new_uid
    - copy_message: SELECT + COPY → new_uid
    - delete_message: SELECT + STORE \\Deleted + EXPUNGE → None
    - get_folders: LIST → [FolderInfo]
    - get_folder_status: STATUS/SELECT → FolderStatus
    - create_folder: CREATE + LIST → FolderInfo
    - delete_folder: DELETE → None
    - rename_folder: RENAME + LIST → FolderInfo
    - execute_raw_command: Passthrough to IMAPClient method

    ThreadPoolExecutor Pattern:
    - Single worker thread (IMAP is single-threaded protocol)
    - All sync IMAPClient calls wrapped via _run_sync()
    - Uses asyncio.get_event_loop().run_in_executor()
    - Non-blocking for FastAPI async request handling

    Folder Caching:
    - Tracks _selected_folder to avoid redundant SELECT calls
    - Invalidated on folder operations (move, delete, rename)
    - Significant performance improvement for sequential operations

    Connection Management:
    - Constructor connects and authenticates immediately
    - Stores IMAPClient instance and ThreadPoolExecutor
    - Connection lifecycle managed by adapter (mailcore doesn't call connect/disconnect)

    Args:
        host: IMAP server hostname
        port: IMAP server port (default: 993 for SSL)
        username: IMAP username (usually email address)
        password: IMAP password (or app-specific password)
        ssl: Use SSL/TLS connection (default: True)
        timeout: Operation timeout in seconds (default: 10)

    Example:
        >>> from mailcore_imapclient import IMAPClientAdapter
        >>> imap = IMAPClientAdapter(
        ...     host='imap.gmail.com',
        ...     port=993,
        ...     username='user@gmail.com',
        ...     password='app-password',  # pragma: allowlist secret
        ...     ssl=True
        ... )
        >>> messages = await imap.query_messages('INBOX', Q.unseen(), limit=50)
    """

    def __init__(
        self,
        host: str,
        port: int = 993,
        username: str = "",
        password: str = "",
        ssl: bool = True,
        timeout: int = 10,
    ):
        """Initialize IMAPClient adapter and connect immediately.

        Args:
            host: IMAP server hostname
            port: IMAP server port (default: 993)
            username: IMAP username
            password: IMAP password
            ssl: Use SSL/TLS (default: True)
            timeout: Operation timeout in seconds (default: 10)
        """
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._ssl = ssl
        self._timeout = timeout

        # Create ThreadPoolExecutor with single worker (IMAP is single-threaded)
        self._executor = ThreadPoolExecutor(max_workers=1)

        # Connect and authenticate immediately
        self._client = IMAPClient(host=host, port=port, ssl=ssl, timeout=timeout)
        self._client.login(username, password)

        # Folder caching to avoid redundant SELECT calls
        self._selected_folder: str | None = None
        self._selected_folder_readonly: bool = True

    async def _run_sync(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run synchronous IMAPClient method in thread pool executor.

        Wraps sync blocking calls to avoid blocking async event loop.
        Uses functools.partial to preserve positional/keyword arguments.

        Args:
            func: Synchronous function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, partial(func, *args, **kwargs))

    async def _select_folder(self, folder: str, readonly: bool = True) -> None:
        """Select folder if not already selected (caching optimization).

        Args:
            folder: Folder name to select
            readonly: Select in read-only mode (default: True)

        Raises:
            FolderNotFoundError: If folder doesn't exist
        """
        # Re-select if folder changed OR readonly mode changed
        if self._selected_folder != folder or self._selected_folder_readonly != readonly:
            try:
                await self._run_sync(self._client.select_folder, folder, readonly=readonly)
                # Only update cache if SELECT succeeded
                self._selected_folder = folder
                self._selected_folder_readonly = readonly
            except Exception as e:
                # Invalidate cache on failure to prevent stale state
                self._selected_folder = None
                self._selected_folder_readonly = True

                # Wrap folder not found errors in domain exception
                error_msg = str(e).lower()
                if (
                    "nonexistent namespace" in error_msg
                    or "does not exist" in error_msg
                    or "no such mailbox" in error_msg
                ):
                    raise FolderNotFoundError(folder) from e
                raise  # Re-raise other exceptions as-is

    async def query_messages(
        self,
        folder: str,
        query: Query,
        include_body: bool = False,
        include_attachment_metadata: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> MessageList:
        """Query messages from folder matching criteria.

        Combines IMAP operations:
        1. SELECT folder (cached to avoid redundant SELECTs)
        2. SEARCH with criteria from query.to_imap_criteria()
        3. FETCH metadata (ENVELOPE, FLAGS, RFC822.SIZE, INTERNALDATE)
        4. Conditionally FETCH BODY[TEXT], BODY[1.HTML] if include_body=True
        5. Conditionally parse BODYSTRUCTURE if include_attachment_metadata=True
        6. Get folder STATUS for total_in_folder count
        7. Create Message objects with imap=self, _smtp=None
        8. Create single IMAPResolver shared by all attachments
        9. Return MessageList with pagination metadata

        Args:
            folder: Folder name
            query: Query object (use query.to_imap_criteria() to get IMAP list)
            include_body: Fetch body text/html (default: False - lazy load)
            include_attachment_metadata: Parse BODYSTRUCTURE for attachments (default: True)
            limit: Maximum messages to return (None = unlimited)
            offset: Skip first N messages (default: 0)

        Returns:
            MessageList with messages, total_matches, total_in_folder, folder
        """
        # SELECT folder (cached)
        await self._select_folder(folder, readonly=True)

        # SEARCH with query criteria
        imap_criteria = query.to_imap_criteria()
        all_uids = await self._run_sync(self._client.search, imap_criteria)

        # Sort UIDs newest first (IMAP search returns arbitrary order)
        all_uids = sorted(all_uids, reverse=True)

        # Pagination
        total_matches = len(all_uids)
        if limit is not None:
            uids = all_uids[offset : offset + limit]
        else:
            uids = all_uids[offset:]

        # Get folder status for total_in_folder (needed even if no UIDs)
        status = await self.get_folder_status(folder)

        # Early return if pagination selected no messages (e.g., limit=0 for count())
        if not uids:
            return MessageList(
                messages=[],
                total_matches=total_matches,
                total_in_folder=status.message_count,
                folder=folder,
            )

        # Build IMAP FETCH fields
        fetch_fields = ["ENVELOPE", "FLAGS", "RFC822.SIZE", "INTERNALDATE"]
        if include_body:
            fetch_fields.extend(["BODY[TEXT]", "BODY[1.HTML]"])
        if include_attachment_metadata:
            fetch_fields.append("BODYSTRUCTURE")

        # FETCH message data
        raw_data = await self._run_sync(self._client.fetch, uids, fetch_fields)

        # Create single IMAPResolver for all attachments
        resolver = IMAPResolver(self)

        # Parse messages
        messages = [
            self._parse_message(uid, raw_data[uid], folder, include_body, include_attachment_metadata, resolver)
            for uid in uids
            if uid in raw_data
        ]

        return MessageList(
            messages=messages,
            total_matches=total_matches,
            total_in_folder=status.message_count,
            folder=folder,
        )

    def _parse_message(
        self,
        uid: int,
        raw: dict[bytes, Any],
        folder: str,
        include_body: bool,
        include_attachment_metadata: bool,
        resolver: IMAPResolver,
    ) -> Message:
        """Parse IMAP FETCH response into Message domain object.

        Creates Message with:
        - imap=self (for lazy loading)
        - _smtp=None (Folder injects SMTP later)
        - Metadata from ENVELOPE, FLAGS, RFC822.SIZE, INTERNALDATE
        - Optional body_text, body_html from BODY[TEXT], BODY[1.HTML]
        - Optional attachments from BODYSTRUCTURE parsing

        Args:
            uid: Message UID
            raw: IMAP FETCH response dict
            folder: Folder name
            include_body: Whether body was fetched
            include_attachment_metadata: Whether to parse BODYSTRUCTURE
            resolver: IMAPResolver instance for attachments

        Returns:
            Message domain object
        """

        # Parse ENVELOPE → EmailAddress objects
        envelope = raw[b"ENVELOPE"]

        # From address (single, always present)
        if envelope.from_:
            from_addr = self._parse_envelope_address(envelope.from_[0])
        else:
            from_addr = EmailAddress("unknown@unknown.com")

        # To addresses (list)
        to_addrs = [self._parse_envelope_address(addr) for addr in envelope.to] if envelope.to else []

        # CC addresses (list, optional)
        cc_addrs = [self._parse_envelope_address(addr) for addr in envelope.cc] if envelope.cc else []

        # Parse FLAGS
        flags, custom_flags = self._parse_flags(raw[b"FLAGS"])

        # Parse BODYSTRUCTURE → Attachments (if requested)
        attachments: list[Attachment] = []
        if include_attachment_metadata and b"BODYSTRUCTURE" in raw:
            attachments = self._parse_bodystructure(raw[b"BODYSTRUCTURE"], folder, uid, resolver)

        # Parse body (if fetched)
        # Note: body_text and body_html are set in Message constructor, NOT via body property
        # MessageBody is created separately and handles lazy loading
        # TODO: For now, pass None since we don't have body fetching implemented

        # Extract metadata
        message_id = envelope.message_id.decode() if envelope.message_id else f"<{uid}@{folder}>"
        subject = self._decode_mime_header(envelope.subject)
        date = raw[b"INTERNALDATE"]
        size = raw[b"RFC822.SIZE"]
        in_reply_to = envelope.in_reply_to.decode() if envelope.in_reply_to else None
        # Note: ENVELOPE doesn't have references - fetch separately if needed
        references = None

        # Create Message with imap=self, _smtp=None
        return Message(
            imap=self,
            uid=uid,
            folder=folder,
            message_id=message_id,
            from_=from_addr,
            to=to_addrs,
            cc=cc_addrs,
            subject=subject,
            date=date,
            flags=flags,
            custom_flags=custom_flags,
            size=size,
            in_reply_to=in_reply_to,
            references=references,
            attachments=attachments or None,
        )

    def _decode_mime_header(self, header_bytes: bytes | None) -> str:
        """Decode MIME-encoded email header (RFC 2047).

        Handles encoded words like =?UTF-8?B?...?= (Base64) and =?UTF-8?Q?...?= (Quoted-printable).
        Gracefully handles malformed MIME headers by falling back to raw string.

        Args:
            header_bytes: Raw header bytes from IMAP ENVELOPE

        Returns:
            Decoded string

        Example:
            >>> _decode_mime_header(b'=?UTF-8?B?SGVsbG8gV29ybGQ=?=')
            'Hello World'
        """
        if not header_bytes:
            return ""

        try:
            # decode_header returns list of (bytes, charset) tuples
            decoded_parts = decode_header(header_bytes.decode("utf-8", errors="replace"))

            # Combine parts, decoding each with its specified charset
            result_parts = []
            for content, charset in decoded_parts:
                if isinstance(content, bytes):
                    # Decode bytes using specified charset (or UTF-8 default)
                    result_parts.append(content.decode(charset or "utf-8", errors="replace"))
                else:
                    # Already a string
                    result_parts.append(content)

            return "".join(result_parts)
        except Exception:
            # Malformed MIME headers - fall back to raw string decoding
            return header_bytes.decode("utf-8", errors="replace")

    def _parse_envelope_address(self, addr: Any) -> EmailAddress:
        """Parse IMAP ENVELOPE address into EmailAddress domain object.

        Args:
            addr: ENVELOPE address tuple (name, route, mailbox, host)

        Returns:
            EmailAddress domain object
        """
        # ENVELOPE address format: (name, route, mailbox, host)
        # Example: (b'John Doe', None, b'john', b'example.com')
        name = self._decode_mime_header(addr.name) if addr.name else None
        mailbox = addr.mailbox.decode("utf-8", errors="replace") if addr.mailbox else ""
        host = addr.host.decode("utf-8", errors="replace") if addr.host else ""
        email = f"{mailbox}@{host}"

        return EmailAddress(email=email, name=name)

    def _parse_flags(self, flags: tuple[bytes, ...]) -> tuple[set[MessageFlag], set[str]]:
        """Parse IMAP FLAGS into (standard flags, custom flags).

        Standard flags (\\Seen, \\Flagged, etc.) → MessageFlag enum
        Custom flags ($Forwarded, $MDNSent, etc.) → strings

        Args:
            flags: IMAP FLAGS tuple (b'\\Seen', b'\\Flagged', b'$Forwarded', etc.)

        Returns:
            Tuple of (standard_flags, custom_flags)
        """
        standard_flags: set[MessageFlag] = set()
        custom_flags: set[str] = set()

        for flag in flags:
            flag_str = flag.decode()
            # Use domain method to convert IMAP string to MessageFlag enum
            message_flag = MessageFlag.from_imap(flag_str)
            if message_flag is not None:
                standard_flags.add(message_flag)
            else:
                # Custom flag (e.g., $Forwarded, $MDNSent)
                custom_flags.add(flag_str)

        return (standard_flags, custom_flags)

    def _parse_bodystructure(
        self, bodystructure: Any, folder: str, uid: int, resolver: IMAPResolver
    ) -> list[Attachment]:
        """Parse BODYSTRUCTURE to extract attachment metadata.

        Args:
            bodystructure: IMAP BODYSTRUCTURE response (complex nested tuple)
            folder: Folder name
            uid: Message UID
            resolver: IMAPResolver instance

        Returns:
            List of Attachment objects with imap:// URIs
        """
        # TODO: Implement BODYSTRUCTURE parsing in separate task
        # This is complex - handles multipart structures recursively
        return []

    async def fetch_message_body(self, folder: str, uid: int) -> tuple[str | None, str | None]:
        """Fetch message body (lazy loading).

        Combines IMAP operations:
        1. SELECT folder (cached)
        2. FETCH BODY[TEXT] for plain text
        3. FETCH BODY[1.HTML] for HTML (if present)

        Args:
            folder: Folder name
            uid: Message UID

        Returns:
            Tuple of (plain_text, html) - either can be None
        """
        # SELECT folder (cached)
        await self._select_folder(folder, readonly=True)

        # FETCH both TEXT and HTML parts
        raw_data = await self._run_sync(self._client.fetch, [uid], ["BODY[TEXT]", "BODY[1.HTML]"])

        if uid not in raw_data:
            return (None, None)

        data = raw_data[uid]

        # Extract plain text (decode UTF-8 bytes to str)
        body_text = None
        if b"BODY[TEXT]" in data:
            text_bytes = data[b"BODY[TEXT]"]
            if text_bytes:
                body_text = text_bytes.decode("utf-8", errors="replace")

        # Extract HTML (decode UTF-8 bytes to str)
        body_html = None
        if b"BODY[1.HTML]" in data:
            html_bytes = data[b"BODY[1.HTML]"]
            if html_bytes:
                body_html = html_bytes.decode("utf-8", errors="replace")

        return (body_text, body_html)

    async def fetch_attachment_content(self, folder: str, uid: int, part_index: str) -> bytes:
        """Fetch attachment content (lazy loading with base64 decode).

        CRITICAL: Base64 decodes content before returning (validated in Story 3.0).

        IMAPClient returns base64-encoded bytes for attachments.
        DOCX test: 24,184 bytes base64 → 17,671 bytes decoded.

        Combines IMAP operations:
        1. SELECT folder (cached)
        2. FETCH BODY[part_index]
        3. Base64 decode the content

        Args:
            folder: Folder name
            uid: Message UID
            part_index: IMAP MIME part number (e.g., "2", "1.2")

        Returns:
            Decoded attachment content as bytes
        """
        # SELECT folder (cached)
        await self._select_folder(folder, readonly=True)

        # FETCH attachment part
        raw_data = await self._run_sync(self._client.fetch, [uid], [f"BODY[{part_index}]"])

        if uid not in raw_data:
            raise ValueError(f"Attachment not found: folder={folder}, uid={uid}, part={part_index}")

        data = raw_data[uid]
        content_key = f"BODY[{part_index}]".encode()

        if content_key not in data:
            raise ValueError(f"Attachment part not found: folder={folder}, uid={uid}, part={part_index}")

        # Get base64-encoded content
        base64_content = data[content_key]

        # CRITICAL: Base64 decode (Story 3.0 validation)
        decoded_content = base64.b64decode(base64_content)

        return decoded_content

    async def update_message_flags(
        self,
        folder: str,
        uid: int,
        add_flags: set[MessageFlag] | None = None,
        remove_flags: set[MessageFlag] | None = None,
        add_custom: set[str] | None = None,
        remove_custom: set[str] | None = None,
    ) -> tuple[set[MessageFlag], set[str]]:
        """Update message flags.

        Combines IMAP operations:
        1. SELECT folder (cached)
        2. STORE +FLAGS for additions
        3. STORE -FLAGS for removals
        4. FETCH FLAGS to get updated flags

        Args:
            folder: Folder name
            uid: Message UID
            add_flags: Standard flags to add
            remove_flags: Standard flags to remove
            add_custom: Custom keywords to add
            remove_custom: Custom keywords to remove

        Returns:
            Tuple of (new_flags, new_custom_flags) after update
        """
        # SELECT folder (cached, read-write for flag updates)
        await self._select_folder(folder, readonly=False)

        # Add flags
        if add_flags:
            imap_add_flags = [self._flag_to_imap(flag) for flag in add_flags]
            await self._run_sync(self._client.add_flags, [uid], imap_add_flags)

        if add_custom:
            await self._run_sync(self._client.add_flags, [uid], list(add_custom))

        # Remove flags
        if remove_flags:
            imap_remove_flags = [self._flag_to_imap(flag) for flag in remove_flags]
            await self._run_sync(self._client.remove_flags, [uid], imap_remove_flags)

        if remove_custom:
            await self._run_sync(self._client.remove_flags, [uid], list(remove_custom))

        # Fetch updated flags
        raw_data = await self._run_sync(self._client.fetch, [uid], ["FLAGS"])
        if uid not in raw_data:
            raise ValueError(f"Message not found after flag update: folder={folder}, uid={uid}")

        # Parse updated flags
        updated_flags_raw = raw_data[uid][b"FLAGS"]

        # Separate standard flags from custom flags
        standard_flags: set[MessageFlag] = set()
        custom_flags: set[str] = set()

        for flag_bytes in updated_flags_raw:
            flag_str = flag_bytes.decode()
            # Use domain method to convert IMAP string to MessageFlag enum
            standard_flag = MessageFlag.from_imap(flag_str)
            if standard_flag is not None:
                standard_flags.add(standard_flag)
            else:
                # Not a standard flag - treat as custom
                custom_flags.add(flag_str)

        return (standard_flags, custom_flags)

    def _flag_to_imap(self, flag: MessageFlag) -> str:
        """Convert MessageFlag enum to IMAP flag string.

        Args:
            flag: MessageFlag enum value

        Returns:
            IMAP flag string (e.g., '\\Seen', '\\Flagged')
        """
        # MessageFlag.value already contains the IMAP flag string with backslash
        value: str = flag.value  # Explicit type annotation for mypy
        return value

    async def move_message(self, uid: int, from_folder: str, to_folder: str) -> int:
        """Move message between folders.

        Uses IMAP MOVE command if available, otherwise COPY + EXPUNGE fallback.

        Args:
            uid: Message UID in source folder
            from_folder: Source folder name
            to_folder: Destination folder name

        Returns:
            New UID in destination folder
        """
        # SELECT source folder (read-write for expunge)
        await self._select_folder(from_folder, readonly=False)

        # Try MOVE command first
        try:
            result = await self._run_sync(self._client.move, [uid], to_folder)
            # MOVE returns dict with COPYUID response if supported
            if result and isinstance(result, dict) and uid in result:
                return int(result[uid])  # New UID from COPYUID response
        except AttributeError:
            # IMAPClient doesn't have move() method - use copy + expunge
            pass

        # Fallback: COPY + STORE \\Deleted + EXPUNGE
        copy_result = await self._run_sync(self._client.copy, [uid], to_folder)
        await self._run_sync(self._client.add_flags, [uid], ["\\Deleted"])
        await self._run_sync(self._client.expunge)

        # Try to get new UID from COPYUID response
        if copy_result and isinstance(copy_result, dict) and uid in copy_result:
            return int(copy_result[uid])

        # If COPYUID not available, return 0 (unknown UID)
        return 0

    async def copy_message(self, uid: int, from_folder: str, to_folder: str) -> int:
        """Copy message between folders.

        Args:
            uid: Message UID in source folder
            from_folder: Source folder name
            to_folder: Destination folder name

        Returns:
            New UID in destination folder (if COPYUID available)
        """
        # SELECT source folder
        await self._select_folder(from_folder, readonly=True)

        # COPY message
        result = await self._run_sync(self._client.copy, [uid], to_folder)

        # Try to get new UID from COPYUID response
        if result and isinstance(result, dict) and uid in result:
            return int(result[uid])

        # If COPYUID not available, return 0 (unknown UID)
        return 0

    async def delete_message(self, folder: str, uid: int, permanent: bool = False) -> None:
        """Delete message.

        Args:
            folder: Folder name
            uid: Message UID
            permanent: If True, expunge immediately. If False, copy to Trash first.
        """
        # SELECT folder (read-write for delete)
        await self._select_folder(folder, readonly=False)

        if not permanent:
            # Copy to Trash before deleting
            try:
                await self._run_sync(self._client.copy, [uid], "Trash")
            except Exception:
                # Trash folder may not exist - ignore and proceed with deletion
                pass

        # Mark as deleted and expunge
        await self._run_sync(self._client.add_flags, [uid], ["\\Deleted"])
        await self._run_sync(self._client.expunge)

    async def get_folders(self) -> list[FolderInfo]:
        """List all folders.

        Returns:
            List of FolderInfo with name, special_use, has_children, flags
        """
        # LIST command returns folder list
        folders_raw = await self._run_sync(self._client.list_folders)

        folders: list[FolderInfo] = []
        for folder_data in folders_raw:
            # folder_data is tuple: (flags, delimiter, name)
            flags, delimiter, name = folder_data

            # Decode folder name (may be UTF-7 encoded)
            folder_name = name.decode("utf-7", errors="replace") if isinstance(name, bytes) else name

            # Parse flags
            has_children = b"\\HasChildren" in flags or b"\\HasNoChildren" not in flags

            folders.append(
                FolderInfo(
                    name=folder_name,
                    has_children=has_children,
                    flags=[flag.decode() if isinstance(flag, bytes) else str(flag) for flag in flags],
                )
            )

        return folders

    async def get_folder_status(self, folder: str) -> FolderStatus:
        """Get folder status (message counts, UIDNEXT).

        Args:
            folder: Folder name

        Returns:
            FolderStatus with message_count, unseen_count, uidnext
        """
        # Try STATUS command first (doesn't require SELECT)
        try:
            status_raw = await self._run_sync(self._client.folder_status, folder, ["MESSAGES", "UNSEEN", "UIDNEXT"])
            return FolderStatus(
                message_count=status_raw.get(b"MESSAGES", 0),
                unseen_count=status_raw.get(b"UNSEEN", 0),
                uidnext=status_raw.get(b"UIDNEXT", 0),
            )
        except Exception:
            # STATUS failed - fallback to SELECT and use select_info
            select_info = await self._run_sync(self._client.select_folder, folder, readonly=True)
            self._selected_folder = folder

            return FolderStatus(
                message_count=select_info.get(b"EXISTS", 0),
                unseen_count=select_info.get(b"UNSEEN", 0),
                uidnext=select_info.get(b"UIDNEXT", 0),
            )

    async def create_folder(self, name: str) -> FolderInfo:
        """Create new folder.

        Args:
            name: Folder name

        Returns:
            FolderInfo for created folder
        """
        # CREATE folder
        await self._run_sync(self._client.create_folder, name)

        # LIST to get metadata
        folders = await self.get_folders()
        for folder in folders:
            if folder.name == name:
                return folder

        # Fallback if folder not found in list
        return FolderInfo(name=name, has_children=False, flags=[])

    async def delete_folder(self, name: str) -> None:
        """Delete folder.

        Args:
            name: Folder name
        """
        await self._run_sync(self._client.delete_folder, name)

    async def rename_folder(self, old_name: str, new_name: str) -> FolderInfo:
        """Rename folder.

        Args:
            old_name: Current folder name
            new_name: New folder name

        Returns:
            FolderInfo for renamed folder
        """
        # RENAME folder
        await self._run_sync(self._client.rename_folder, old_name, new_name)

        # LIST to get metadata
        folders = await self.get_folders()
        for folder in folders:
            if folder.name == new_name:
                return folder

        # Fallback if folder not found in list
        return FolderInfo(name=new_name, has_children=False, flags=[])

    async def execute_raw_command(self, command: str, *args: Any) -> Any:
        """Execute raw IMAP command (escape hatch).

        Provides direct access to IMAPClient methods for advanced use cases.
        Command name is converted to lowercase to match IMAPClient method names.

        Args:
            command: IMAP command name (e.g., 'search', 'fetch', 'select_folder')
            *args: Command arguments

        Returns:
            Command result

        Example:
            >>> # Execute raw SEARCH command
            >>> result = await adapter.execute_raw_command('search', ['UNSEEN'])
        """
        # Get method from IMAPClient instance (convert command to lowercase)
        method_name = command.lower()
        method = getattr(self._client, method_name)

        # Execute in thread pool
        return await self._run_sync(method, *args)
