#ifndef AOIDE_SOCKET_H
#define AOIDE_SOCKET_H

#include "aoide_engine.h"
#include "aoide_json.h"
#include <sys/socket.h>
#include <sys/un.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#include <iostream>

namespace aoide {

inline BackendClient::BackendClient(const std::string &socketPath)
    : socketPath_(socketPath) {}

inline BackendClient::~BackendClient() {
    disconnect();
}

inline bool BackendClient::connect() {
    if (connected_) return true;

    // 清理上一次死连接的 fd，避免 fd 泄漏
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }

    fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd_ < 0) {
        std::cerr << "[Aoide] socket() failed: " << strerror(errno) << std::endl;
        return false;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, socketPath_.c_str(), sizeof(addr.sun_path) - 1);

    if (::connect(fd_, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        std::cerr << "[Aoide] connect() to " << socketPath_
                  << " failed: " << strerror(errno) << std::endl;
        ::close(fd_);
        fd_ = -1;
        return false;
    }

    connected_ = true;
    std::cout << "[Aoide] Connected to backend: " << socketPath_ << std::endl;
    return true;
}

inline void BackendClient::disconnect() {
    stopReceiveLoop();
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
    connected_ = false;
}

inline bool BackendClient::isConnected() const {
    return connected_;
}

inline bool BackendClient::sendRaw(const uint8_t *data, size_t len) {
    if (!connected_ || fd_ < 0) return false;

    uint32_t netLen = htonl(static_cast<uint32_t>(len));
    {
        std::lock_guard<std::mutex> lock(mutex_);
        ssize_t sent = ::send(fd_, &netLen, 4, MSG_NOSIGNAL);
        if (sent != 4) return false;
        if (len > 0) {
            sent = ::send(fd_, data, len, MSG_NOSIGNAL);
            if (static_cast<size_t>(sent) != len) return false;
        }
    }
    return true;
}

inline bool BackendClient::sendAudio(const std::vector<int16_t> &pcmData) {
    if (pcmData.empty()) return true;

    std::string header = "{\"type\":\"audio\",\"samples\":" +
                         std::to_string(pcmData.size()) +
                         ",\"rate\":16000}";
    header.push_back('\0');

    std::vector<uint8_t> msg(header.begin(), header.end());
    const uint8_t *pcm = reinterpret_cast<const uint8_t *>(pcmData.data());
    msg.insert(msg.end(), pcm, pcm + pcmData.size() * sizeof(int16_t));

    return sendRaw(msg.data(), msg.size());
}

inline bool BackendClient::sendCommand(const std::string &command) {
    std::string msg = command;
    return sendRaw(reinterpret_cast<const uint8_t *>(msg.data()), msg.size());
}

inline void BackendClient::setResultCallback(ResultCallback callback) {
    callback_ = std::move(callback);
}

inline void BackendClient::startReceiveLoop() {
    // 确保加入任何已结束的旧线程（避免 std::terminate）
    if (receiveThread_.joinable()) {
        receiveThread_.join();
    }
    receiving_.store(true);
    receiveThread_ = std::thread(&BackendClient::receiveLoop, this);
}

inline void BackendClient::stopReceiveLoop() {
    receiving_.store(false);
    // shutdown 读取端以唤醒可能阻塞在 recv() 上的接收线程
    if (fd_ >= 0) {
        ::shutdown(fd_, SHUT_RD);
    }
    if (receiveThread_.joinable()) {
        receiveThread_.join();
    }
    // 重置连接状态，避免 connect() 因 connected_ 仍为 true
    // 而跳过创建新 socket，导致复用已 shutdown 的旧 fd
    connected_.store(false);
}

inline void BackendClient::receiveLoop() {
    const size_t BUF_SIZE = 65536;
    std::vector<uint8_t> buf(BUF_SIZE);

    while (receiving_.load() && connected_.load()) {
        uint32_t netLen = 0;
        ssize_t n = ::recv(fd_, &netLen, 4, MSG_WAITALL);
        if (n <= 0) {
            if (n == 0) {
                std::cerr << "[Aoide] Backend disconnected" << std::endl;
            } else if (errno != EINTR) {
                std::cerr << "[Aoide] recv() failed: " << strerror(errno) << std::endl;
            }
            connected_.store(false);
            break;
        }

        uint32_t msgLen = ntohl(netLen);
        if (msgLen == 0 || msgLen > BUF_SIZE) continue;

        buf.resize(msgLen);
        size_t totalRead = 0;
        while (totalRead < msgLen) {
            n = ::recv(fd_, buf.data() + totalRead, msgLen - totalRead, 0);
            if (n <= 0) {
                connected_.store(false);
                break;
            }
            totalRead += static_cast<size_t>(n);
        }

        if (totalRead < msgLen) break;

        // 安全地构造字符串（处理可能的 null 字节）
        std::string msg(reinterpret_cast<const char*>(buf.data()), msgLen);

        auto extractField = jsonField;

        std::string type = extractField(msg, "type");
        std::string text = extractField(msg, "text");

        if (callback_ && !type.empty()) {
            callback_(type, text, msg);
        }
    }
}

} // namespace aoide

#endif // AOIDE_SOCKET_H
