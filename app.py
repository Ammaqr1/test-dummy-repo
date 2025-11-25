import streamlit as st
import websockets

# Page configuration
st.set_page_config(
    page_title="Simple Streamlit App",
    page_icon="🚀",
    layout="wide"
)

# Title
st.title("🚀 Welcome to My Streamlit App")
st.markdown("---")

# Sidebar
with st.sidebar:
    st.header("Navigation")
    page = st.radio(
        "Choose a page:",
        ["Home", "Calculator", "Form"]
    )

# Main content based on selected page
if page == "Home":
    st.header("Home Page")
    st.write("This is a simple Streamlit application.")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Features")
        st.write("✅ Interactive UI")
        st.write("✅ Multiple pages")
        st.write("✅ Form inputs")
        st.write("✅ Calculator")
    
    with col2:
        st.subheader("About")
        st.info("This app demonstrates basic Streamlit functionality including forms, calculations, and interactive widgets.")

elif page == "Calculator":
    st.header("Simple Calculator")
    
    col1, col2 = st.columns(2)
    
    with col1:
        num1 = st.number_input("Enter first number", value=0.0)
    
    with col2:
        num2 = st.number_input("Enter second number", value=0.0)
    
    operation = st.selectbox(
        "Select operation",
        ["Add", "Subtract", "Multiply", "Divide"]
    )
    
    if st.button("Calculate"):
        if operation == "Add":
            result = num1 + num2
        elif operation == "Subtract":
            result = num1 - num2
        elif operation == "Multiply":
            result = num1 * num2
        elif operation == "Divide":
            if num2 != 0:
                result = num1 / num2
            else:
                st.error("Cannot divide by zero!")
                result = None
        
        if result is not None:
            st.success(f"Result: {result}")

elif page == "Form":
    st.header("Contact Form")
    
    with st.form("contact_form"):
        name = st.text_input("Name")
        email = st.text_input("Email")
        message = st.text_area("Message")
        age = st.slider("Age", 0, 100, 25)
        submitted = st.form_submit_button("Submit")
        
        if submitted:
            if name and email and message:
                st.success(f"Thank you {name}! Your message has been submitted.")
                st.json({
                    "Name": name,
                    "Email": email,
                    "Message": message,
                    "Age": age
                })
            else:
                st.warning("Please fill in all fields!")

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using Streamlit")

