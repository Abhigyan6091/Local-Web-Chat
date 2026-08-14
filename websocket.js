// websocket.js
// ─────────────────────────────────────────────────────────────────────────────
// WebSocket layer — Private Rooms & Global Chat support
// ─────────────────────────────────────────────────────────────────────────────

window.WS = (function () {
  var host = window.location.hostname || 'localhost';
  var WS_URL = 'ws://' + host + ':8000/ws';

  var _ws             = null;
  var _onMessage      = function () {};
  var _onStatusChange = function () {};
  var _myUsername     = '';

  function connect(username) {
    _myUsername = username;
    _onStatusChange('connecting');

    _ws = new WebSocket(WS_URL);

    _ws.onopen = function () {
      _onStatusChange('connected');
      _ws.send(JSON.stringify({ type: 'join', username: _myUsername }));
    };

    _ws.onclose = function () {
      _onStatusChange('disconnected');
      _ws = null;
    };

    _ws.onerror = function () {
      _onStatusChange('disconnected');
    };

    _ws.onmessage = function (e) {
      var event;
      try {
        event = JSON.parse(e.data);
      } catch (err) {
        console.error('WS: failed to parse message', e.data);
        return;
      }
      _onMessage(event);
    };
  }

  function sendMessage(text) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'message', text: text }));
    }
  }

  function createRoom(roomName) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'create_room', name: roomName }));
    }
  }

  function joinRoomByCode(code) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'join_room', code: code }));
    }
  }

  function switchRoom(roomId) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'switch_room', roomId: roomId }));
    }
  }

  function disconnect() {
    if (_ws) {
      _ws.close();
      _ws = null;
    }
    _onStatusChange('disconnected');
  }

  function setOnMessage(handler) {
    _onMessage = handler;
  }

  function setOnStatusChange(handler) {
    _onStatusChange = handler;
  }

  return {
    connect: connect,
    sendMessage: sendMessage,
    createRoom: createRoom,
    joinRoomByCode: joinRoomByCode,
    switchRoom: switchRoom,
    disconnect: disconnect,
    setOnMessage: setOnMessage,
    setOnStatusChange: setOnStatusChange
  };
})();
