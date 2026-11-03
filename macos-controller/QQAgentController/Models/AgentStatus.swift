import Foundation

struct LogItem: Codable, Identifiable, Equatable {
    var id: String { "\(time)-\(text.hashValue)" }
    var time: String
    var level: String?
    var text: String
}

struct DialogueItem: Codable, Identifiable, Equatable {
    var id: String { "\(time)-\(senderId)-\(userText.hashValue)" }
    var time: String
    var source: String
    var senderId: String
    var userText: String
    var character: String
    var reply: String
    var voice: Bool?

    enum CodingKeys: String, CodingKey {
        case time
        case source
        case senderId = "sender_id"
        case userText = "user_text"
        case character
        case reply
        case voice
    }
}

struct VoiceServicesStatus: Codable, Equatable {
    var api9880: Bool?
    var studio9876: Bool?
    var ttsUrl: String?
    var studioUrl: String?

    enum CodingKeys: String, CodingKey {
        case api9880 = "api_9880"
        case studio9876 = "studio_9876"
        case ttsUrl = "tts_url"
        case studioUrl = "studio_url"
    }
}

struct VoiceRefItem: Codable, Identifiable, Equatable {
    var id: String { "\(character)-\(name)" }
    var character: String
    var name: String
    var promptText: String?
    var promptLang: String?
    var exists: Bool?
    var isDefault: Bool?

    enum CodingKeys: String, CodingKey {
        case character
        case name
        case promptText = "prompt_text"
        case promptLang = "prompt_lang"
        case exists
        case isDefault = "is_default"
    }
}

struct VoiceModelItem: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var version: String
    var ready: Bool
}

struct AgentStatus: Codable, Equatable {
    var globalActive: Bool
    var activeCharacter: String
    var activeGroups: [String]
    var comfortTargets: [String]
    var voiceEnabled: Bool?
    var ttsEnabled: Bool?
    var voiceOnlyReply: Bool?
    var voiceEmotionDirectorEnabled: Bool?
    var voiceMode: String?
    var voiceRefName: String?
    var voiceModel: String?
    var voiceModels: [VoiceModelItem]?
    var voiceSpeed: Double?
    var voiceTemperature: Double?
    var voiceTopP: Double?
    var voiceServices: VoiceServicesStatus?
    var voiceRefs: [VoiceRefItem]?
    var characterRefs: [String]?
    var botConnected: Bool?
    var connectedBots: [String]?
    var recentLogs: [LogItem]?
    var recentDialogues: [DialogueItem]?

    enum CodingKeys: String, CodingKey {
        case globalActive = "global_active"
        case activeCharacter = "active_character"
        case activeGroups = "active_groups"
        case comfortTargets = "comfort_targets"
        case voiceEnabled = "voice_enabled"
        case ttsEnabled = "tts_enabled"
        case voiceOnlyReply = "voice_only_reply"
        case voiceEmotionDirectorEnabled = "voice_emotion_director_enabled"
        case voiceMode = "voice_mode"
        case voiceRefName = "voice_ref_name"
        case voiceModel = "voice_model"
        case voiceModels = "voice_models"
        case voiceSpeed = "voice_speed"
        case voiceTemperature = "voice_temperature"
        case voiceTopP = "voice_top_p"
        case voiceServices = "voice_services"
        case voiceRefs = "voice_refs"
        case characterRefs = "character_refs"
        case botConnected = "bot_connected"
        case connectedBots = "connected_bots"
        case recentLogs = "recent_logs"
        case recentDialogues = "recent_dialogues"
    }

    static let empty = AgentStatus(
        globalActive: false,
        activeCharacter: "none",
        activeGroups: [],
        comfortTargets: [],
        voiceEnabled: false,
        ttsEnabled: true,
        voiceOnlyReply: false,
        voiceEmotionDirectorEnabled: false,
        voiceMode: "emotion_auto",
        voiceRefName: "default",
        voiceSpeed: 1.0,
        voiceTemperature: 0.85,
        voiceTopP: 0.9,
        voiceServices: nil,
        voiceRefs: [],
        characterRefs: [],
        botConnected: false,
        connectedBots: [],
        recentLogs: [],
        recentDialogues: []
    )
}

struct APIAck: Codable {
    var code: Int
    var globalActive: Bool?
    var activeCharacter: String?
    var ttsEnabled: Bool?
    var voiceOnlyReply: Bool?
    var voiceEmotionDirectorEnabled: Bool?
    var voiceMode: String?
    var voiceRefName: String?
    var voiceSpeed: Double?
    var voiceTemperature: Double?
    var voiceTopP: Double?
    var message: String?

    enum CodingKeys: String, CodingKey {
        case code
        case globalActive = "global_active"
        case activeCharacter = "active_character"
        case ttsEnabled = "tts_enabled"
        case voiceOnlyReply = "voice_only_reply"
        case voiceEmotionDirectorEnabled = "voice_emotion_director_enabled"
        case voiceMode = "voice_mode"
        case voiceRefName = "voice_ref_name"
        case voiceSpeed = "voice_speed"
        case voiceTemperature = "voice_temperature"
        case voiceTopP = "voice_top_p"
        case message
    }
}
