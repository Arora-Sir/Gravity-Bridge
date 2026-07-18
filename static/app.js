document.addEventListener('DOMContentLoaded', () => {
    // UI Elements
    const chatMessages = document.getElementById('chat-messages');
    const chatGreeting = document.getElementById('chat-greeting');
    const chatInput = document.getElementById('chat-input');
    const sendBtn = document.getElementById('send-btn');
    const attachBtn = document.getElementById('attach-btn');
    const fileAttachmentsInput = document.getElementById('file-attachments-input');
    const attachmentPreview = document.getElementById('attachment-preview');
    
    const settingsToggleBtn = document.getElementById('settings-toggle-btn');
    const settingsPanel = document.getElementById('settings-panel');
    const apiKeyInput = document.getElementById('api-key-input');
    const modelSelect = document.getElementById('model-select');
    const saveSettingsBtn = document.getElementById('save-settings-btn');
    const clearChatBtn = document.getElementById('clear-chat-btn');
    
    const historyList = document.getElementById('history-list');
    const historyCount = document.getElementById('history-count');
    const toast = document.getElementById('toast');
    const starterCards = document.querySelectorAll('.starter-card');

    // App State
    let pendingFiles = [];
    let chatHistory = []; // Array of {role: 'user'|'model', parts: [{text: '...'}]}
    let isProcessing = false;

    // Load Configuration
    if (localStorage.getItem('gravity_api_key')) {
        apiKeyInput.value = localStorage.getItem('gravity_api_key');
    }
    if (localStorage.getItem('gravity_model')) {
        modelSelect.value = localStorage.getItem('gravity_model');
    }

    // Toggle settings panel
    settingsToggleBtn.addEventListener('click', () => {
        settingsPanel.classList.toggle('hidden');
    });

    // Save Settings
    saveSettingsBtn.addEventListener('click', () => {
        localStorage.setItem('gravity_api_key', apiKeyInput.value.trim());
        localStorage.setItem('gravity_model', modelSelect.value);
        settingsPanel.classList.add('hidden');
        showToast('Settings saved');
    });

    // Clear chat
    clearChatBtn.addEventListener('click', () => {
        if (confirm('Clear current conversation?')) {
            clearChatState();
            showToast('Chat cleared');
        }
    });

    function clearChatState() {
        chatHistory = [];
        chatMessages.innerHTML = '';
        chatMessages.appendChild(chatGreeting);
        pendingFiles = [];
        updateAttachmentPreview();
    }

    // Auto-grow input textarea height
    chatInput.addEventListener('input', () => {
        chatInput.style.height = 'auto';
        chatInput.style.height = (chatInput.scrollHeight - 4) + 'px';
    });

    // Trigger file chooser
    attachBtn.addEventListener('click', () => {
        if (isProcessing) return;
        fileAttachmentsInput.click();
    });

    // Select files
    fileAttachmentsInput.addEventListener('change', (e) => {
        const files = Array.from(e.target.files);
        files.forEach(file => {
            if (!pendingFiles.some(f => f.name === file.name && f.size === file.size)) {
                pendingFiles.push(file);
            }
        });
        fileAttachmentsInput.value = '';
        updateAttachmentPreview();
    });

    // Render attachment badges
    function updateAttachmentPreview() {
        if (pendingFiles.length === 0) {
            attachmentPreview.classList.add('hidden');
            attachmentPreview.innerHTML = '';
            return;
        }

        attachmentPreview.innerHTML = '';
        attachmentPreview.classList.remove('hidden');

        pendingFiles.forEach((file, index) => {
            const badge = document.createElement('div');
            badge.className = 'preview-badge';
            
            let icon = '📄';
            const ext = file.name.split('.').pop().toLowerCase();
            if (ext === 'zip' || ext === 'rar') icon = '📦';
            else if (ext === 'xlsx' || ext === 'xls') icon = '📊';
            else if (ext === 'csv') icon = '📈';
            else if (['png', 'jpg', 'jpeg'].includes(ext)) icon = '🖼️';
            
            badge.innerHTML = `
                <span class="preview-badge-icon">${icon}</span>
                <span class="preview-badge-name" title="${file.name}">${file.name}</span>
                <button class="preview-badge-delete" data-idx="${index}">&times;</button>
            `;
            
            badge.querySelector('.preview-badge-delete').addEventListener('click', (e) => {
                const idx = parseInt(e.target.getAttribute('data-idx'));
                pendingFiles.splice(idx, 1);
                updateAttachmentPreview();
            });
            
            attachmentPreview.appendChild(badge);
        });
    }

    function showToast(message) {
        toast.textContent = message;
        toast.classList.remove('hidden');
        setTimeout(() => toast.classList.add('show'), 50);
        
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.classList.add('hidden'), 350);
        }, 2200);
    }

    // Append chat bubbles
    function appendMessageBubble(role, contentText, fileNames = []) {
        if (chatGreeting.parentElement) {
            chatGreeting.remove();
        }

        const turnDiv = document.createElement('div');
        turnDiv.className = `msg-turn ${role}`;
        
        let filesTagHtml = '';
        if (fileNames.length > 0) {
            filesTagHtml = '<div class="msg-bubble-attachments">' + 
                fileNames.map(name => `<span class="preview-badge" style="margin-top: 6px; display: inline-flex;"><font color="#ffcb30">📎 ${name}</font></span>`).join(' ') + 
                '</div>';
        }

        let bodyHtml = '';
        if (role === 'model' && typeof marked !== 'undefined') {
            bodyHtml = marked.parse(contentText);
        } else {
            bodyHtml = `<p>${contentText.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br>")}</p>`;
        }

        turnDiv.innerHTML = `
            <div class="msg-bubble">
                ${bodyHtml}
                ${filesTagHtml}
            </div>
        `;
        
        chatMessages.appendChild(turnDiv);
        scrollChatToBottom();
    }

    function scrollChatToBottom() {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    // Starter Prompt Cards
    starterCards.forEach(card => {
        card.addEventListener('click', () => {
            const prompt = card.getAttribute('data-prompt');
            chatInput.value = prompt;
            sendMessage();
        });
    });

    sendBtn.addEventListener('click', sendMessage);

    chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    async function sendMessage() {
        if (isProcessing) return;
        
        const messageText = chatInput.value.trim();
        if (!messageText && pendingFiles.length === 0) {
            showToast('Enter a message or attach files');
            return;
        }

        isProcessing = true;
        chatInput.value = '';
        chatInput.style.height = 'auto';

        const fileNames = pendingFiles.map(f => f.name);
        const filesToUpload = [...pendingFiles];
        pendingFiles = [];
        updateAttachmentPreview();

        // 1. Add user bubble
        appendMessageBubble('user', messageText || '[Uploaded attachments]', fileNames);

        // 2. Add loading indicator
        const statusBadge = document.createElement('div');
        statusBadge.id = 'agent-status-badge';
        statusBadge.className = 'tool-notification';
        statusBadge.innerHTML = `<span class="tool-dot"></span><span>Laptop Agent is processing files & executing tools...</span>`;
        chatMessages.appendChild(statusBadge);
        scrollChatToBottom();

        try {
            // 3. Upload files to laptop first
            if (filesToUpload.length > 0) {
                const uploadData = new FormData();
                filesToUpload.forEach(f => {
                    uploadData.append('files', f);
                });
                
                const uploadRes = await fetch('/api/upload', {
                    method: 'POST',
                    body: uploadData
                });
                
                if (!uploadRes.ok) {
                    throw new Error('File upload failed. Unable to parse files.');
                }
            }

            // 4. Construct payload
            let promptInstruction = messageText;
            if (!promptInstruction) {
                promptInstruction = "I have uploaded these files. Analyze their structure and content.";
            }

            chatHistory.push({
                role: 'user',
                parts: [{ text: promptInstruction }]
            });

            const headers = { 'Content-Type': 'application/json' };
            const customKey = localStorage.getItem('gravity_api_key');
            if (customKey) {
                headers['X-Gemini-API-Key'] = customKey;
            }

            const targetModel = localStorage.getItem('gravity_model') || 'gemini-3.5-flash';

            // 5. Send Chat request
            const response = await fetch('/api/chat', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify({
                    history: chatHistory,
                    model: targetModel
                })
            });

            statusBadge.remove();

            if (!response.ok) {
                const errData = await response.json();
                throw new Error(errData.detail || 'Agent execution failed');
            }

            const data = await response.json();
            
            // 6. Display model response
            appendMessageBubble('model', data.result);
            
            chatHistory.push({
                role: 'model',
                parts: [{ text: data.result }]
            });

            loadHistory();

        } catch (err) {
            statusBadge.remove();
            appendMessageBubble('model', `❌ **Error:** ${err.message}`);
            showToast('Execution failed');
        } finally {
            isProcessing = false;
        }
    }

    // Load History log list
    async function loadHistory() {
        try {
            const res = await fetch('/api/history');
            if (!res.ok) throw new Error('Failed to load history');
            const data = await res.json();
            
            historyCount.textContent = data.length;
            
            if (data.length === 0) {
                historyList.innerHTML = '<div class="empty-history">No past chat records found on this laptop.</div>';
                return;
            }
            
            historyList.innerHTML = '';
            data.forEach(item => {
                const div = document.createElement('div');
                div.className = 'history-item';
                
                const timeString = new Date(item.timestamp).toLocaleString();
                const filesCount = item.uploaded_files ? item.uploaded_files.length : 0;
                const previewText = item.prompt || '[File Analysis]';
                
                div.innerHTML = `
                    <div class="history-info">
                        <div class="history-name">💬 ${previewText.length > 35 ? previewText.substring(0, 35) + '...' : previewText}</div>
                        <div class="history-meta">${timeString} ${filesCount > 0 ? `• 📎 ${filesCount} files` : ''}</div>
                    </div>
                    <button class="history-delete-btn" aria-label="Delete logs">&times;</button>
                `;
                
                div.querySelector('.history-info').addEventListener('click', () => {
                    if (chatGreeting.parentElement) {
                        chatGreeting.remove();
                    }
                    chatMessages.innerHTML = '';
                    appendMessageBubble('user', item.prompt, item.uploaded_files || []);
                    appendMessageBubble('model', item.result);
                    scrollChatToBottom();
                    showToast('Loaded past analysis');
                });
                
                div.querySelector('.history-delete-btn').addEventListener('click', async (e) => {
                    e.stopPropagation();
                    if (confirm('Delete this chat log record from laptop?')) {
                        try {
                            const delRes = await fetch(`/api/history/${item.id}`, { method: 'DELETE' });
                            if (delRes.ok) {
                                showToast('Record deleted');
                                loadHistory();
                            }
                        } catch (err) {
                            showToast('Error deleting log');
                        }
                    }
                });
                
                historyList.appendChild(div);
            });
        } catch (err) {
            console.error('History load error:', err);
        }
    }

    loadHistory();
});
