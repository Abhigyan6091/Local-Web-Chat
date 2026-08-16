// main.js
// ─────────────────────────────────────────────────────────────────────────────
// Handles UI logic: screen switching, message rendering, multi-room state,
// modal dialogs, and wiring up the WS (websocket.js) layer.
// ─────────────────────────────────────────────────────────────────────────────

// ── DOM References ─────────────────────────────────────────────────────────
var joinScreen         = document.getElementById('join-screen');
var chatScreen         = document.getElementById('chat-screen');
var joinForm           = document.getElementById('join-form');
var usernameInput      = document.getElementById('username-input');
var joinError          = document.getElementById('join-error');

var messagesArea       = document.getElementById('messages-area');
var messageForm        = document.getElementById('message-form');
var messageInput       = document.getElementById('message-input');
var sendBtn            = document.getElementById('send-btn');

var roomsList          = document.getElementById('rooms-list');
var usersList          = document.getElementById('users-list');
var statusBadge        = document.getElementById('status-badge');
var headerStatus       = document.getElementById('header-status');
var chatRoomName       = document.getElementById('chat-room-name');
var roomCodeBadge      = document.getElementById('room-code-badge');
var roomCodeVal        = document.getElementById('room-code-val');
var userCountLabel     = document.getElementById('user-count-label');
var myUsernameDisp     = document.getElementById('my-username-display');
var myAvatar           = document.getElementById('my-avatar');
var leaveBtn           = document.getElementById('leave-btn');
var sidebarToggle      = document.getElementById('sidebar-toggle');
var sidebar            = document.getElementById('sidebar');

// Modal DOM References
var btnCreateRoomTrig  = document.getElementById('btn-create-room-trigger');
var btnJoinRoomTrig    = document.getElementById('btn-join-room-trigger');
var createRoomModal    = document.getElementById('create-room-modal');
var joinRoomModal      = document.getElementById('join-room-modal');
var createRoomForm     = document.getElementById('create-room-form');
var joinRoomForm       = document.getElementById('join-room-form');
var createRoomNameInp  = document.getElementById('create-room-name-input');
var joinRoomCodeInp    = document.getElementById('join-room-code-input');
var btnCancelCreate    = document.getElementById('btn-cancel-create');
var btnCancelJoin      = document.getElementById('btn-cancel-join');
var joinRoomError      = document.getElementById('join-room-error');
var toastNotif         = document.getElementById('toast-notification');

// ── App State ──────────────────────────────────────────────────────────────
var myUsername = '';
var currentRoomId = 'global';
var joinedRooms = {
  global: { id: 'global', name: 'General Room', code: null, is_private: false }
};
var onlineUsers = {};

// ── Avatar colors ──────────────────────────────────────────────────────────
var AVATAR_COLORS = [
  '#7c3aed','#db2777','#0891b2','#d97706',
  '#16a34a','#dc2626','#0284c7','#9333ea',
];

function avatarColor(name) {
  var hash = 0;
  for (var i = 0; i < name.length; i++) {
    hash = (hash * 31 + name.charCodeAt(i)) & 0xffffffff;
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function initials(name) {
  return name.slice(0, 2).toUpperCase();
}

function formatTime(ts) {
  var d = ts ? new Date(ts) : new Date();
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// ── Screen switching ────────────────────────────────────────────────────────
function showScreen(name) {
  joinScreen.classList.toggle('active', name === 'join');
  chatScreen.classList.toggle('active', name === 'chat');
}

// ── Toast notifications ──────────────────────────────────────────────────────
function showToast(msg) {
  toastNotif.textContent = msg;
  toastNotif.hidden = false;
  setTimeout(function () {
    toastNotif.hidden = true;
  }, 3500);
}

// ── Render system message ────────────────────────────────────────────────────
function appendSystemMessage(text) {
  var el = document.createElement('div');
  el.className = 'msg-system';
  el.textContent = text;
  messagesArea.appendChild(el);
  scrollToBottom();
}

// ── Render chat bubble ───────────────────────────────────────────────────────
// verified/tampered are optional — undefined for messages sent live in this
// session (server marks those verified=true), and set from DB history for
// old messages that went through decrypt + signature check on the backend.
function appendChatMessage(sender, text, isMe, timestamp, verified, tampered) {
  var row = document.createElement('div');
  row.className = 'msg-row ' + (isMe ? 'me' : 'other');

  var meta = document.createElement('div');
  meta.className = 'msg-meta';

  if (!isMe) {
    var senderEl = document.createElement('span');
    senderEl.className = 'sender';
    senderEl.textContent = sender;
    meta.appendChild(senderEl);
  }

  var timeEl = document.createElement('span');
  timeEl.textContent = formatTime(timestamp);
  meta.appendChild(timeEl);

  if (tampered) {
    var tamperBadge = document.createElement('span');
    tamperBadge.className = 'msg-badge tampered';
    tamperBadge.title = 'Message integrity check failed';
    tamperBadge.textContent = '⚠ tampered';
    meta.appendChild(tamperBadge);
  } else if (verified) {
    var verifiedBadge = document.createElement('span');
    verifiedBadge.className = 'msg-badge verified';
    verifiedBadge.title = 'Signature verified';
    verifiedBadge.textContent = '✓ verified';
    meta.appendChild(verifiedBadge);
  }

  var bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.textContent = text;

  row.appendChild(meta);
  row.appendChild(bubble);
  messagesArea.appendChild(row);
  scrollToBottom();
}

function scrollToBottom() {
  messagesArea.scrollTop = messagesArea.scrollHeight;
}

// ── Render Rooms list ────────────────────────────────────────────────────────
function renderRooms() {
  roomsList.innerHTML = '';

  for (var rId in joinedRooms) {
    if (!joinedRooms.hasOwnProperty(rId)) continue;
    var room = joinedRooms[rId];
    var isActive = rId === currentRoomId;

    var li = document.createElement('li');
    li.className = 'room-item' + (isActive ? ' active' : '');
    li.setAttribute('data-room-id', rId);

    var icon = document.createElement('span');
    icon.className = 'room-icon';
    icon.textContent = room.is_private ? '🔒' : '🌐';

    var nameEl = document.createElement('span');
    nameEl.className = 'room-name';
    nameEl.textContent = room.name;

    li.appendChild(icon);
    li.appendChild(nameEl);

    if (room.is_private && room.code) {
      var tag = document.createElement('span');
      tag.className = 'room-code-tag';
      tag.textContent = room.code;
      li.appendChild(tag);
    }

    li.addEventListener('click', (function (targetId) {
      return function () {
        if (targetId !== currentRoomId) {
          WS.switchRoom(targetId);
        }
      };
    })(rId));

    roomsList.appendChild(li);
  }
}

// ── Render Online Users list ─────────────────────────────────────────────────
function renderUsers() {
  usersList.innerHTML = '';
  var count = 0;
  for (var username in onlineUsers) {
    if (!onlineUsers.hasOwnProperty(username)) continue;
    count++;
    var isMe = username === myUsername;

    var li = document.createElement('li');
    li.className = 'user-item' + (isMe ? ' me' : '');

    var av = document.createElement('span');
    av.className = 'avatar';
    av.style.background = avatarColor(username);
    av.textContent = initials(username);

    var nameEl = document.createElement('span');
    nameEl.textContent = username + (isMe ? ' (you)' : '');

    var dot = document.createElement('span');
    dot.className = 'online-dot';

    li.appendChild(av);
    li.appendChild(nameEl);
    li.appendChild(dot);
    usersList.appendChild(li);
  }
  userCountLabel.textContent = count + ' online';
}

// ── Status indicator ─────────────────────────────────────────────────────────
function setStatus(status) {
  var labels = { connecting: 'Connecting…', connected: 'Connected', disconnected: 'Disconnected' };
  statusBadge.textContent = labels[status] || status;
  statusBadge.className   = 'status-badge ' + status;
  headerStatus.className  = 'header-status ' + status;

  var enabled = status === 'connected';
  messageInput.disabled = !enabled;
  sendBtn.disabled      = !enabled;
}

// ── Handle incoming WS events ────────────────────────────────────────────────
function handleEvent(event) {
  if (event.type === 'room_entered') {
    currentRoomId = event.room.id;
    joinedRooms[event.room.id] = event.room;
    renderRooms();

    // Set header room details
    chatRoomName.textContent = (event.room.is_private ? '🔒 ' : '🌐 ') + event.room.name;
    if (event.room.is_private && event.room.code) {
      roomCodeVal.textContent = event.room.code;
      roomCodeBadge.hidden = false;
    } else {
      roomCodeBadge.hidden = true;
    }

    // Reset message area & render DB history
    messagesArea.innerHTML = '<div class="messages-start-label"><span>— Start of conversation —</span></div>';
    if (event.history && event.history.length > 0) {
      event.history.forEach(function (msg) {
        appendChatMessage(msg.sender, msg.text, msg.sender === myUsername, msg.timestamp, msg.verified, msg.tampered);
      });
    }

    // Set room online users
    onlineUsers = {};
    onlineUsers[myUsername] = true;
    if (event.users) {
      event.users.forEach(function (u) { onlineUsers[u] = true; });
    }
    renderUsers();

  } else if (event.type === 'room_created') {
    showToast('Room created! Code: ' + event.room.code);

  } else if (event.type === 'message') {
    if (event.roomId === currentRoomId) {
      appendChatMessage(event.sender, event.text, event.sender === myUsername, event.timestamp, event.verified, event.tampered);
    }

  } else if (event.type === 'user_joined') {
    if (!event.roomId || event.roomId === currentRoomId) {
      onlineUsers[event.username] = true;
      renderUsers();
      appendSystemMessage(event.username + ' joined the room');
    }

  } else if (event.type === 'user_left') {
    if (!event.roomId || event.roomId === currentRoomId) {
      delete onlineUsers[event.username];
      renderUsers();
      appendSystemMessage(event.username + ' left the room');
    }

  } else if (event.type === 'error') {
    if (!joinRoomModal.hidden) {
      joinRoomError.textContent = event.message;
      joinRoomError.hidden = false;
    } else {
      showToast('Error: ' + event.message);
    }
  }
}

// ── Join Form Submit ─────────────────────────────────────────────────────────
joinForm.addEventListener('submit', function (e) {
  e.preventDefault();
  var raw = usernameInput.value.trim();

  if (raw.length < 3 || raw.length > 20) {
    showError('Username must be 3–20 characters.');
    return;
  }
  if (!/^[a-zA-Z0-9_]+$/.test(raw)) {
    showError('Only letters, numbers, and underscores allowed.');
    return;
  }

  hideError();
  myUsername = raw;
  startChat();
});

function showError(msg) {
  joinError.textContent = msg;
  joinError.hidden = false;
}

function hideError() {
  joinError.hidden = true;
}

function startChat() {
  myUsernameDisp.textContent = myUsername;
  myAvatar.style.background  = avatarColor(myUsername);
  myAvatar.textContent       = initials(myUsername);

  showScreen('chat');

  WS.setOnMessage(handleEvent);
  WS.setOnStatusChange(setStatus);

  setStatus('connecting');
  WS.connect(myUsername);
}

// ── Message Form Submit ──────────────────────────────────────────────────────
messageForm.addEventListener('submit', function (e) {
  e.preventDefault();
  var text = messageInput.value.trim();
  if (!text) return;
  WS.sendMessage(text);
  messageInput.value = '';
  messageInput.focus();
});

// ── Room Modals Logic ────────────────────────────────────────────────────────
btnCreateRoomTrig.addEventListener('click', function () {
  createRoomNameInp.value = '';
  createRoomModal.hidden = false;
  createRoomNameInp.focus();
});

btnCancelCreate.addEventListener('click', function () {
  createRoomModal.hidden = true;
});

createRoomForm.addEventListener('submit', function (e) {
  e.preventDefault();
  var name = createRoomNameInp.value.trim();
  if (name) {
    WS.createRoom(name);
    createRoomModal.hidden = true;
  }
});

btnJoinRoomTrig.addEventListener('click', function () {
  joinRoomCodeInp.value = '';
  joinRoomError.hidden = true;
  joinRoomModal.hidden = false;
  joinRoomCodeInp.focus();
});

btnCancelJoin.addEventListener('click', function () {
  joinRoomModal.hidden = true;
});

joinRoomForm.addEventListener('submit', function (e) {
  e.preventDefault();
  var code = joinRoomCodeInp.value.trim().toUpperCase();
  if (code) {
    WS.joinRoomByCode(code);
    joinRoomModal.hidden = true;
  }
});

// ── Copy Room Code Badge ─────────────────────────────────────────────────────
roomCodeBadge.addEventListener('click', function () {
  var code = roomCodeVal.textContent;
  if (code && navigator.clipboard) {
    navigator.clipboard.writeText(code).then(function () {
      showToast('Room code ' + code + ' copied to clipboard! 📋');
    });
  }
});

// ── Leave button ─────────────────────────────────────────────────────────────
leaveBtn.addEventListener('click', function () {
  WS.disconnect();
  setStatus('disconnected');
  setTimeout(function () {
    onlineUsers = {};
    joinedRooms = { global: { id: 'global', name: 'General Room', code: null, is_private: false } };
    currentRoomId = 'global';
    messagesArea.innerHTML = '<div class="messages-start-label"><span>— Start of conversation —</span></div>';
    usernameInput.value = '';
    showScreen('join');
  }, 1500);
});

// ── Sidebar toggle (mobile) ───────────────────────────────────────────────────
sidebarToggle.addEventListener('click', function () {
  sidebar.classList.toggle('open');
});

messagesArea.addEventListener('click', function () {
  if (sidebar.classList.contains('open')) {
    sidebar.classList.remove('open');
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────
showScreen('join');
usernameInput.focus();

// ── Keep header pinned when mobile keyboard opens ──────────────────────────
function setAppHeight() {
  var h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  document.documentElement.style.setProperty('--app-height', h + 'px');
}
setAppHeight();
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', setAppHeight);
  window.visualViewport.addEventListener('scroll', setAppHeight);
} else {
  window.addEventListener('resize', setAppHeight);
}