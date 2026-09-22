#ifndef YUHUANG_JSON_H
#define YUHUANG_JSON_H

#include <nlohmann/json.hpp>
#include <string>

namespace yuhuang {
inline std::string jsonField(const std::string &source, const std::string &name) {
    const auto value = nlohmann::json::parse(source, nullptr, false);
    if (!value.is_object() || !value.contains(name)) return {};
    const auto &field = value[name];
    if (field.is_string()) return field.get<std::string>();
    if (field.is_number() || field.is_boolean()) return field.dump();
    return {};
}
} // namespace yuhuang
#endif
