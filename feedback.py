from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label


class FeedbackItem(BoxLayout):
    def __init__(self, msg, name, **kwargs):
        super().__init__(**kwargs)
        self.orientation = "vertical"
        self.size_hint_y = None
        self.height = 80
        self.padding = 5
        self.spacing = 5

        # Add labels
        self.add_widget(Label(text=f"Name: {name}", size_hint_y=None, height=30))
        self.add_widget(Label(text=f"Feedback: {msg}", size_hint_y=None, height=30))


class FeedbackScreen(BoxLayout):
    # Example feedback list
    feedbacks = [
        {"name": "Alice", "feedback_message": "Great app!"},
        {"name": "Bob", "feedback_message": "Needs improvement."},
        {"name": "Charlie", "feedback_message": "UI is clean and simple."}
    ]


class FeedbackApp(App):
    def build(self):
        return FeedbackScreen()


if __name__ == "__main__":
    FeedbackApp().run()
